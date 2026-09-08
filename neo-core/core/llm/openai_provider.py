"""OpenAI provider implementation."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessage

from core.llm.provider import CompletionResponse, LLMProvider
from shared import LLMProviderError, Message, StreamChunk, StreamEventType, ToolCall, ToolSchema

logger = logging.getLogger(__name__)

# 429 = TPM/RPM da org estourado; renova em segundos. Backoff curto porque há um
# usuário esperando no chat — melhor 3 tentativas rápidas que um erro na cara dele.
_RATE_LIMIT_RETRIES = 3
_RATE_LIMIT_BACKOFF = (2.0, 5.0, 10.0)

#: Modelos que já recusaram ``tool_choice`` forçado, por (modelo, pin de provedor).
#:
#: O 404 do OpenRouter com pin (ver ``_rejects_forced_tool_choice``) é um fato de
#: INFRA, não um erro transitório: o endpoint do Baidu serve tools mas não aceita
#: ``required``, e vai recusar de novo no próximo turno. Sem esta memória, todo
#: turno com tool forçada (``force_tool_first``, skill autocarregada) paga uma
#: ida e volta de ~2s só para ouvir o mesmo não — medido na session-d94dc42f.
#:
#: Vive pelo processo. É a granularidade certa: a capacidade só muda quando o
#: gateway muda o roteamento do endpoint, e o neo-core reinicia a cada `--reload`,
#: o que já dá a re-sondagem de graça.
_FORCED_TOOL_CHOICE_UNSUPPORTED: set[tuple[str, str]] = set()


def _pinned_provider(params: dict[str, Any]) -> str:
    """Slug do provedor de infra fixado nesta chamada (ou "" quando livre).

    O pin mora em ``extra_body["provider"]["order"]`` — escrito pelo
    ``OpenRouterProvider`` a partir do sufixo ``@slug``. Entra na chave do cache
    acima porque a recusa é do ENDPOINT, não do modelo: o mesmo
    ``deepseek/deepseek-v4-flash`` aceita ``required`` quando o gateway pode
    escolher outro endpoint.
    """
    pin = (params.get("extra_body") or {}).get("provider") or {}
    if not isinstance(pin, dict):
        return ""
    order = pin.get("order") or []
    return str(order[0]) if order else ""


def _is_transient(error: Exception) -> bool:
    """Falha de transporte/servidor que vale repetir (5xx, timeout, conexão caída).

    Existe porque o ``complete()`` desliga o retry do SDK (ver lá) para ser o
    DONO ÚNICO da política. Só desligar deixaria o turno mais frágil do que era:
    o SDK também repetia 5xx e queda de conexão, não só 429. Isto recompõe essa
    metade dentro da mesma escada de backoff.
    """
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if isinstance(status, int) and status >= 500:
        return True
    return isinstance(error, (httpx.TimeoutException, httpx.TransportError)) or type(
        error
    ).__name__ in ("APIConnectionError", "APITimeoutError", "InternalServerError")


def _content_for(message: Message) -> Any:
    """Corpo da mensagem no formato da API — texto puro ou partes multimodais.

    Só vira lista de partes quando a mensagem carrega imagem: mandar
    ``[{"type": "text", ...}]`` em TODA mensagem é válido na API mas muda o
    corpo de todo turno do Neo sem necessidade. Imagem chega como base64 puro
    (o que o CDP devolve) ou como data URL já pronta.
    """
    images = getattr(message, "images", None)
    if not images:
        return message.content
    parts: list[dict[str, Any]] = [{"type": "text", "text": message.content or ""}]
    for image in images:
        if not image:
            continue
        url = image if image.startswith("data:") else f"data:image/jpeg;base64,{image}"
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def _is_rate_limit(error: Exception) -> bool:
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status == 429:
        return True
    return type(error).__name__ == "RateLimitError" or "429" in str(error)


def forces_tool_call(tool_choice: Any) -> bool:
    """O ``tool_choice`` EXIGE uma tool (vs. deixar o modelo escolher)?

    Vale tanto para ``"required"`` quanto para o dict que nomeia uma função —
    as duas formas obrigam, e as duas batem nas mesmas restrições de API.
    """
    return tool_choice is not None and tool_choice not in ("auto", "none")


def _rejects_forced_tool_choice(error: Exception) -> bool:
    """A API recusou a REQUISIÇÃO por causa do ``tool_choice`` forçado?

    Caso conhecido: DeepSeek v4 em thinking mode devolve 400 "Thinking mode does
    not support this tool_choice" (o DeepSeekProvider já evita isso desligando o
    thinking). Isto aqui é a rede: se um provider/modelo novo tiver a mesma
    limitação, o turno degrada para ``auto`` em vez de morrer — perder a
    GARANTIA da tool é ruim, perder o turno inteiro é pior.
    """
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    text = str(error).lower()
    if (status == 400 or "400" in text) and "tool_choice" in text:
        return True
    # O OpenRouter recusa pelo ROTEAMENTO, não pelo parâmetro, e por isso não diz
    # "tool_choice" em lugar nenhum. Com um pin de provedor (``vendor/modelo@slug``)
    # ele filtra os endpoints por compatibilidade de tool e o pin zera o que sobrou:
    # sai um 404 "No endpoints found". Medido em 2026-09-02 com
    # deepseek/deepseek-v4-flash@baidu — o funil vai de 17 endpoints para 6
    # ("Filter by Tool Compatibility") e daí para 0 ("Filter by Fallback"), porque
    # o endpoint do Baidu serve tools mas não aceita tool_choice="required".
    # Mesma limitação de sempre, outro vocabulário; sem reconhecê-la aqui, todo
    # turno com tool forçada (force_tool_first, skill de finanças) morria.
    return (status == 404 or "404" in text) and "filter by tool compatibility" in text


class OpenAICompletionResponse(CompletionResponse):
    """Wrapper para resposta da OpenAI."""

    def __init__(self, response: ChatCompletion) -> None:
        self._response = response
        self._message: ChatCompletionMessage = response.choices[0].message

    def get_content(self) -> str | None:
        """Retorna o conteúdo da mensagem."""
        return self._message.content

    def get_tool_calls(self) -> list[ToolCall]:
        """Extrai tool calls da resposta."""
        if not self._message.tool_calls:
            return []

        tool_calls: list[ToolCall] = []
        for tc in self._message.tool_calls:
            tool_calls.append(
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=json.loads(tc.function.arguments),
                )
            )
        return tool_calls

    def finish_reason(self) -> str:
        """Retorna o finish_reason."""
        return self._response.choices[0].finish_reason


class OpenAIProvider(LLMProvider):
    """Provider para OpenAI (GPT-4, GPT-3.5, etc)."""

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        super().__init__(api_key, **kwargs)
        self.client = AsyncOpenAI(
            api_key=api_key,
            http_client=httpx.AsyncClient(
                limits=httpx.Limits(
                    max_keepalive_connections=5,
                    max_connections=10,
                    keepalive_expiry=60,
                ),
            ),
        )

    @property
    def _client_no_sdk_retry(self) -> AsyncOpenAI:
        """O mesmo client, com a escada de retry INTERNA do SDK desligada.

        Só para o ``complete()``, que tem escada própria — ver o comentário lá.
        ``with_options`` reaproveita o ``httpx.AsyncClient`` (e portanto o pool de
        conexões keep-alive), então isto não abre conexão nova; o cache é só para
        não reconstruir o wrapper a cada chamada.

        A memoização é contra o ``self.client`` ATUAL de propósito: as subclasses
        (OpenRouter, DeepSeek) trocam ``self.client`` depois de ``__init__`` para
        apontar para outra base_url. Guardar o derivado em ``__init__`` deixaria
        um client apontando para ``api.openai.com`` dentro do provider errado —
        falha caríssima de achar, porque a chave até existe.
        """
        cached = getattr(self, "_no_sdk_retry_cache", None)
        if cached is None or cached[0] is not self.client:
            cached = (self.client, self.client.with_options(max_retries=0))
            self._no_sdk_retry_cache = cached
        return cached[1]

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> CompletionResponse:
        """Realiza completion com OpenAI."""
        try:
            # `disable_thinking` é uma INTENÇÃO do chamador ("preciso da saída
            # estruturada, não do raciocínio"), não um parâmetro da API. Cada
            # provider que sabe traduzi-la consome antes de chegar aqui (ver
            # DeepSeekProvider); para os demais é ruído e precisa sumir — indo
            # junto no corpo, a API responde 400 e derruba o turno.
            kwargs.pop("disable_thinking", None)

            # Idem: intenção do chamador, não parâmetro da API. `True` = "não gaste
            # a escada de backoff com 429, levanta na primeira e me deixa consertar
            # a ROTA". Quem usa é o OpenRouterProvider quando há um pin de provedor
            # trancado: dormir 17s esperando um pool lotado esvaziar é pior que
            # soltar o pin na hora e deixar o gateway achar um endpoint livre.
            bail_on_rate_limit = bool(kwargs.pop("bail_on_rate_limit", False))

            # Converte mensagens para formato OpenAI
            openai_messages = [
                {"role": msg.role.value, "content": _content_for(msg)} for msg in messages
            ]

            # Prepara parâmetros
            params: dict[str, Any] = {
                "model": model or "gpt-4-turbo-preview",
                "messages": openai_messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                **kwargs,
            }

            # Adiciona tools se fornecidas. O chamador pode impor `tool_choice`
            # (ex.: "required" para obrigar uma tool no primeiro round) — sem tools
            # o parâmetro é inválido na API, então cai fora.
            if tools:
                params["tools"] = self.format_tools_for_api(tools)
                params.setdefault("tool_choice", "auto")
            else:
                params.pop("tool_choice", None)

            # Já sabemos que este endpoint recusa tool forçada? Então não gaste a
            # ida e volta para descobrir de novo — desce direto para "auto", que é
            # onde o retry abaixo terminaria de qualquer jeito.
            unsupported_key = (str(params["model"]), _pinned_provider(params))
            if (
                forces_tool_call(params.get("tool_choice"))
                and unsupported_key in _FORCED_TOOL_CHOICE_UNSUPPORTED
            ):
                logger.debug(
                    "[OpenAI] %r no provedor %r já recusou tool forçada antes — "
                    "indo direto de 'auto' (a obrigação fica no texto da mensagem)",
                    unsupported_key[0], unsupported_key[1] or "livre",
                )
                params["tool_choice"] = "auto"

            # Rate limit (429) é transitório: o TPM da org se renova em segundos. Sem
            # retry, o turno morria virando "Erro ao processar: 429" — que ainda por
            # cima entra no histórico e envenena os turnos seguintes.
            #
            # `max_retries=0` desliga a escada INTERNA do SDK só nesta chamada: com
            # as duas ligadas cada tentativa daqui virava 3 requests (o SDK repete
            # 2x por conta própria), 12 requests por round — que além de somarem
            # ~5s de sono invisível ainda alimentavam o rate limiter do qual
            # estávamos tentando escapar. O `stream_complete` continua com o padrão
            # do SDK: lá não existe escada nossa para assumir o lugar.
            client = self._client_no_sdk_retry
            last_error: Exception | None = None
            downgraded_tool_choice = False
            for attempt in range(_RATE_LIMIT_RETRIES + 1):
                try:
                    response = await client.chat.completions.create(**params)
                    return OpenAICompletionResponse(response)
                except Exception as e:  # noqa: BLE001 — só 429/5xx/tool_choice são retentáveis
                    last_error = e
                    # Modelo que não aceita tool forçada: refaz com "auto" em vez
                    # de derrubar o turno. Uma vez só — se falhar de novo, o erro
                    # é outro. Não consome tentativa de rate limit.
                    if (
                        not downgraded_tool_choice
                        and forces_tool_call(params.get("tool_choice"))
                        and _rejects_forced_tool_choice(e)
                    ):
                        downgraded_tool_choice = True
                        _FORCED_TOOL_CHOICE_UNSUPPORTED.add(unsupported_key)
                        logger.warning(
                            "[OpenAI] modelo %r no provedor %r recusou tool_choice=%r — "
                            "refazendo com 'auto' (a obrigação da tool fica só no texto "
                            "da mensagem) e memorizando para os próximos turnos",
                            unsupported_key[0], unsupported_key[1] or "livre",
                            params.get("tool_choice"),
                        )
                        params["tool_choice"] = "auto"
                        continue
                    # Pin trancado + 429: o chamador consegue consertar a ROTA, e
                    # isso vale mais que qualquer espera. Sai já, sem dormir.
                    if bail_on_rate_limit and _is_rate_limit(e):
                        logger.info(
                            "[OpenAI] 429 com rota fixada — devolvendo na hora para o "
                            "chamador liberar o fallback (sem gastar o backoff)"
                        )
                        raise
                    retryable = _is_rate_limit(e) or _is_transient(e)
                    if not retryable or attempt == _RATE_LIMIT_RETRIES:
                        raise
                    delay = _RATE_LIMIT_BACKOFF[attempt]
                    logger.warning(
                        "[OpenAI] %s — retry %d/%d em %.1fs",
                        "rate limit (429)" if _is_rate_limit(e) else f"falha transitória ({type(e).__name__})",
                        attempt + 1, _RATE_LIMIT_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
            raise last_error  # type: ignore[misc]  # inalcançável: o loop ou retorna ou levanta

        except Exception as e:
            raise LLMProviderError(
                f"Erro na completion OpenAI: {str(e)}",
                details={"provider": "openai", "model": model},
            ) from e

    async def stream_complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
        temperature: float = 0.9,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """Streaming completion with OpenAI — yields StreamChunk events."""
        try:
            openai_messages = [
                {"role": msg.role.value, "content": _content_for(msg)} for msg in messages
            ]

            params: dict[str, Any] = {
                "model": model or "gpt-4o",
                "messages": openai_messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True,
                **kwargs,
            }

            if tools:
                params["tools"] = self.format_tools_for_api(tools)
                params["tool_choice"] = "auto"

            stream = await self.client.chat.completions.create(**params)

            # Track tool calls being assembled across chunks
            tool_calls_buffer: dict[int, dict[str, str]] = {}

            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                finish_reason = chunk.choices[0].finish_reason if chunk.choices else None

                if delta is None:
                    continue

                # Text content
                if delta.content:
                    yield StreamChunk(
                        event=StreamEventType.TOKEN,
                        content=delta.content,
                    )

                # Tool calls
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_buffer:
                            tool_calls_buffer[idx] = {
                                "id": tc.id or "",
                                "name": "",
                                "arguments": "",
                            }
                            if tc.id:
                                tool_calls_buffer[idx]["id"] = tc.id

                        if tc.function:
                            if tc.function.name:
                                tool_calls_buffer[idx]["name"] = tc.function.name
                                yield StreamChunk(
                                    event=StreamEventType.TOOL_CALL_START,
                                    tool_call_id=tool_calls_buffer[idx]["id"],
                                    tool_name=tc.function.name,
                                )
                            if tc.function.arguments:
                                tool_calls_buffer[idx]["arguments"] += tc.function.arguments
                                yield StreamChunk(
                                    event=StreamEventType.TOOL_CALL_ARGS,
                                    tool_call_id=tool_calls_buffer[idx]["id"],
                                    tool_arguments_delta=tc.function.arguments,
                                )

                # Stream end
                if finish_reason:
                    # Emit tool_call_end for completed tool calls
                    if finish_reason == "tool_calls":
                        for idx, tc_data in tool_calls_buffer.items():
                            yield StreamChunk(
                                event=StreamEventType.TOOL_CALL_END,
                                tool_call_id=tc_data["id"],
                                tool_name=tc_data["name"],
                                tool_arguments_delta=tc_data["arguments"],
                            )

                    yield StreamChunk(
                        event=StreamEventType.DONE,
                        finish_reason=finish_reason,
                    )

        except Exception as e:
            yield StreamChunk(
                event=StreamEventType.ERROR,
                content=str(e),
            )
            raise LLMProviderError(
                f"Erro no streaming OpenAI: {str(e)}",
                details={"provider": "openai", "model": model},
            ) from e

    def format_tools_for_api(self, tools: list[ToolSchema]) -> list[dict[str, Any]]:
        """Formata tools para OpenAI function calling."""
        formatted_tools: list[dict[str, Any]] = []

        for tool in tools:
            # Monta properties e required
            properties: dict[str, Any] = {}
            required: list[str] = []

            for param_name, param in tool.parameters.items():
                properties[param_name] = {
                    "type": param.type,
                    "description": param.description,
                }
                if param.enum:
                    properties[param_name]["enum"] = param.enum

                if param.required:
                    required.append(param_name)

            # Formato OpenAI
            formatted_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": {
                            "type": "object",
                            "properties": properties,
                            "required": required,
                        },
                    },
                }
            )

        return formatted_tools
