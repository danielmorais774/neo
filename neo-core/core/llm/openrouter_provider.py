"""OpenRouter provider — OpenAI-compatible gateway to many model vendors.

OpenRouter (https://openrouter.ai) expõe uma API 100% compatível com a OpenAI,
apenas mudando o ``base_url`` e usando IDs de modelo no formato ``vendor/modelo``
(ex.: ``google/gemini-3.1-flash-lite``, ``anthropic/claude-3.5-sonnet``).

Vantagem: uma única API key dá acesso a dezenas de modelos (Google, Anthropic,
Meta, Mistral, etc.) com fallback automático e roteamento por preço/disponibilidade.
"""

import logging
from typing import Any

import httpx
from openai import AsyncOpenAI

from core.llm.openai_provider import OpenAIProvider, is_upstream_failure
from core.llm.provider import LLMProvider
from shared import LLMProviderError, Message, StreamChunk, StreamEventType, ToolSchema

logger = logging.getLogger(__name__)

#: Tradução de ``disable_thinking`` para o campo do OpenRouter.
#:
#: Muitos modelos servidos aqui raciocinam POR PADRÃO, e o gateway não aplica o
#: ``max_tokens`` ao orçamento de raciocínio. Medido em 2026-08-01 com
#: ``qwen/qwen3.5-flash-02-23`` (upstream Alibaba) no prompt do appraiser de afeto,
#: ``max_tokens=220``: **11-20s e 2.040-3.995 tokens de reasoning** para emitir ~180
#: chars de JSON. Com este campo: ~0,6s e zero reasoning.
#:
#: Isto é o que faltava para a intenção ``disable_thinking`` valer aqui: como este
#: provider herda o ``complete`` da OpenAI — que DESCARTA a flag por ser ruído para
#: a API dela —, quem pedia saída curta e estruturada (o ``intent_router`` pede em
#: toda classificação) estava pagando o raciocínio inteiro sem saber.
_REASONING_DISABLED = {"reasoning": {"enabled": False}}

#: Modelos que RECUSAM ``reasoning.enabled=false`` — o gateway responde
#:     400 "Reasoning is mandatory for this endpoint and cannot be disabled."
#: e o turno morre em "deu erro do meu lado" (medido 2026-11-06 com
#: ``liquid/lfm-3-1.7b``, cadastrado pelo painel). ``disable_thinking`` é política
#: do sistema inteiro (cérebro, classificador, afeto, navegador…), então um modelo
#: assim quebraria TODAS essas chamadas. Aprende-se na primeira recusa: refaz uma
#: vez sem o campo e, dali em diante, nem manda — custa um 400 por modelo por
#: processo, em vez de uma flag a mais no cadastro que ninguém saberia marcar.
_REASONING_MANDATORY: set[str] = set()


def _provedores_ignorados() -> tuple[str, ...]:
    """``[openrouter] provedores_ignorados`` do neo.toml (vazio se ilegível)."""
    try:
        from shared.ajustes import ajustes

        return tuple(p for p in ajustes().openrouter.provedores_ignorados if p)
    except Exception:  # noqa: BLE001 — ajuste ilegível não derruba a chamada
        return ()


def _exige_raciocinio(model: str) -> bool:
    """O modelo recusa ``reasoning.enabled=false``?

    O conjunto aprendido mora na memória do processo e some a cada ``--reload``:
    cada reinício pagava um 400 na primeira chamada de cada modelo (o primeiro
    turno da session-a20f4722 levou um). Os já conhecidos vêm de
    ``[openrouter] raciocinio_obrigatorio`` no ``neo.toml``.
    """
    if model in _REASONING_MANDATORY:
        return True
    try:
        from shared.ajustes import ajustes

        return model in ajustes().openrouter.raciocinio_obrigatorio
    except Exception:  # noqa: BLE001 — ajuste ilegível não derruba a chamada
        return False


def _raciocinio_com_teto(teto: int) -> dict[str, Any]:
    """O campo que LIMITA o raciocínio de um modelo que não deixa desligá-lo.

    Sem isto, ``disable_thinking`` num modelo assim virava "raciocínio sem
    limite": na session-a20f4722 o classificador (GLM-5.3-flash) raciocinou até
    estourar 2048 tokens, voltou vazio, e a nova tentativa com 4096 levou o turno
    a 44 s só de classificação. Medido em 13/11 com 30 pedidos reais: com
    ``max_tokens`` o MiniMax m3.1-flash fecha com zero tokens de raciocínio.
    """
    return {"reasoning": {"max_tokens": int(teto)}}


def _is_reasoning_mandatory(exc: BaseException | None) -> bool:
    while exc is not None:
        if "reasoning is mandatory" in str(exc).lower():
            return True
        exc = exc.__cause__
    return False

#: Separador do "pin de provedor" nos IDs deste catálogo: ``vendor/modelo@slug``.
#:
#: O OpenRouter serve o MESMO modelo por vários provedores de infra, e eles não
#: são intercambiáveis: mudam preço, quantização, contexto e até quais parâmetros
#: aceitam (o ``deepseek/deepseek-v4-flash``, sondado em 2026-09-02, tem 17
#: endpoints — de $0,0679 a $0,08386 por M de entrada, e só alguns aceitam
#: ``tool_choice="required"``). O ID do modelo sozinho não expressa essa escolha.
#:
#: Ela também não cabia em nenhum campo existente: ``(provider, model)`` é o par
#: que atravessa o ``user_preference`` no banco, o metadata do gRPC do desktop e
#: os selects dos dois frontends. Em vez de abrir um terceiro campo em todas
#: essas camadas, a escolha viaja dentro do próprio ID e é traduzida AQUI — no
#: único lugar que fala com a API — para o campo ``provider`` do corpo.
_PROVIDER_PIN_SEP = "@"


class OpenRouterProvider(OpenAIProvider):
    """Provider para OpenRouter (acesso unificado a múltiplos vendors).

    A API do OpenRouter é compatível com a OpenAI — apenas muda o ``base_url`` e
    os IDs de modelo passam a ser ``vendor/modelo``:
      - google/gemini-3.1-flash-lite  → rápido e barato, ótimo p/ classificação
      - google/gemini-2.0-flash       → bom custo-benefício
      - anthropic/claude-3.5-sonnet   → tarefas complexas
      - meta-llama/llama-3.3-70b-instruct → open-weights
      - deepseek/deepseek-v4.1-flash@fireworks → 1M de contexto (pin de provedor)

    Os headers opcionais ``HTTP-Referer``/``X-Title`` identificam o app no
    leaderboard do OpenRouter (não são obrigatórios).
    """

    DEFAULT_MODEL = "google/gemini-3.1-flash-lite"
    BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        # Bypass OpenAIProvider.__init__ to use OpenRouter base_url + keep-alive pooling
        LLMProvider.__init__(self, api_key, **kwargs)
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or self.BASE_URL,
            default_headers={
                "HTTP-Referer": "https://github.com/neo-core",
                "X-Title": "Neo Core",
            },
            http_client=httpx.AsyncClient(
                limits=httpx.Limits(
                    max_keepalive_connections=5,
                    max_connections=10,
                    keepalive_expiry=60,
                ),
            ),
        )
        self.model = self.DEFAULT_MODEL

    @staticmethod
    def _split_provider_pin(model: str, kwargs: dict[str, Any]) -> str:
        """Separa ``vendor/modelo@slug`` e fixa o roteamento no slug pedido.

        Devolve o ID limpo — o sufixo é convenção nossa e a API responderia 404
        com ele — e escreve ``provider.order`` com ``allow_fallbacks=False``.
        O ``allow_fallbacks`` importa: sem ele o slug vira mera *preferência* e o
        gateway cai em outro provedor no primeiro erro, que é exatamente o que
        quem escolheu um provedor específico (por preço ou quantização) não quer
        — e falharia calado, cobrando outro preço.

        Um ``extra_body["provider"]`` vindo do chamador vence, mesma regra do
        ``_apply_reasoning_pref``: quem foi explícito manda.
        """
        bare, _, slug = model.rpartition(_PROVIDER_PIN_SEP)
        if not bare or not slug:
            return model
        extra = dict(kwargs.get("extra_body") or {})
        extra.setdefault("provider", {"order": [slug], "allow_fallbacks": False})
        kwargs["extra_body"] = extra
        logger.debug("[OpenRouter] %s fixado no provedor %r (sem fallback)", bare, slug)
        return bare

    @staticmethod
    def _apply_provider_ignore(kwargs: dict[str, Any]) -> None:
        """Tira do roteamento os provedores de ``[openrouter] provedores_ignorados``.

        Vai em ``provider.ignore``: o gateway segue escolhendo entre todos os
        outros, nada fica fixado. Roda DEPOIS de ``_split_provider_pin``: chamada
        com provedor fixado (``order``/``only``) já escolheu e fica como está. Um
        ``ignore`` que o chamador já mandou é somado, não trocado.
        """
        ignorar = _provedores_ignorados()
        if not ignorar:
            return
        extra = dict(kwargs.get("extra_body") or {})
        pref = extra.get("provider")
        if isinstance(pref, dict) and (pref.get("order") or pref.get("only")):
            return
        pref = dict(pref) if isinstance(pref, dict) else {}
        atuais = list(pref.get("ignore") or [])
        pref["ignore"] = atuais + [p for p in ignorar if p not in atuais]
        extra["provider"] = pref
        kwargs["extra_body"] = extra

    @staticmethod
    def _pin_is_locked(kwargs: dict[str, Any]) -> bool:
        """Esta chamada tem um pin de provedor TRANCADO (sem fallback)?

        É a condição que torna o backoff por 429 um desperdício: com o fallback
        trancado não existe segunda rota para a espera destravar — o pool do
        provedor fixado ou esvazia sozinho, ou não. Quem sabe consertar é o
        ``_relax_provider_pin`` logo abaixo, e ele precisa da vez.
        """
        pin = (kwargs.get("extra_body") or {}).get("provider") or {}
        return isinstance(pin, dict) and pin.get("allow_fallbacks") is False

    @staticmethod
    def _relax_provider_pin(kwargs: dict[str, Any], exc: Exception) -> bool:
        """Provedor fixado LOTADO: mantém a preferência, libera a saída.

        Devolve True quando afrouxou (e vale repetir a chamada). O pin vira
        `order` SEM `allow_fallbacks=False`: o gateway continua tentando o
        provedor escolhido PRIMEIRO e só sai dele quando ele não atende.

        Vale só para "está ocupado" (429/502/503) ou "caiu no meio" (erro dentro
        de uma resposta 200 — ver `is_upstream_failure`), nunca para erro de
        conteúdo — 404 de compatibilidade de tool, por exemplo, tem tratamento
        próprio em `_rejects_forced_tool_choice` e afrouxar ali esconderia a causa.

        Existe porque um laço de passos é a carga que estoura RPM: medido em
        2026-09-03 com `deepseek/deepseek-v4-flash@baidu`, o pool compartilhado
        do Baidu devolveu `rpm_rate_limit_exceeded` e, com o fallback trancado,
        as 3 tentativas de rate limit da OpenAIProvider esgotaram e a tarefa
        inteira do navegador morreu no passo 1. Perder o provedor preferido é
        ruim; perder a tarefa é pior — mesmo julgamento do downgrade de
        `tool_choice` em `openai_provider`. O desvio vai para o log com o preço
        em jogo: silêncio aqui é que seria inaceitável.
        """
        extra = kwargs.get("extra_body") or {}
        pin = extra.get("provider") or {}
        if not isinstance(pin, dict) or pin.get("allow_fallbacks") is not False:
            return False  # sem pin trancado: nada a afrouxar (nem 2ª tentativa)
        text = str(exc).lower()
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        busy = (
            status in (429, 502, 503)
            or "429" in text
            or "rate_limit" in text
            or "rate-limited" in text
            or "temporarily" in text
            or "overloaded" in text
            # A causa real mora no `__cause__`: o LLMProviderError só carrega o
            # texto, e o do Baidu caído ("JSON error injected into SSE stream")
            # não tem palavra nenhuma de lotação.
            or is_upstream_failure(exc)
            or is_upstream_failure(exc.__cause__)
        )
        if not busy:
            return False
        kwargs["extra_body"] = {
            **extra,
            "provider": {**pin, "allow_fallbacks": True},
        }
        logger.warning(
            "[OpenRouter] provedor fixado %r está lotado — refazendo com fallback "
            "liberado (outro provedor pode atender, com OUTRO preço): %s",
            (pin.get("order") or ["?"])[0], str(exc)[:200],
        )
        return True

    @staticmethod
    def _apply_reasoning_pref(
        kwargs: dict[str, Any], model: str, teto: int | None = None
    ) -> None:
        """Traduz a intenção ``disable_thinking`` para o campo ``reasoning`` da API.

        Consome a flag ANTES de subir para ``OpenAIProvider``, que a descartaria.
        Um ``extra_body`` vindo do chamador vence — quem foi explícito manda.

        Modelo que exige raciocínio não aceita o desligamento. Com ``teto`` (o
        ``teto_raciocinio`` de quem chama), ele recebe o raciocínio LIMITADO; sem
        teto, não recebe campo nenhum, como antes — o navegador, por exemplo, não
        passa teto porque a decisão dele depende do raciocínio.
        """
        if not kwargs.pop("disable_thinking", False):
            return
        extra = kwargs.get("extra_body") or {}
        if _exige_raciocinio(model):
            if teto:
                kwargs["extra_body"] = {**_raciocinio_com_teto(teto), **extra}
                logger.debug("[OpenRouter] %s: raciocínio limitado a %d tokens", model, teto)
            return
        kwargs["extra_body"] = {**_REASONING_DISABLED, **extra}
        logger.debug("[OpenRouter] reasoning desligado nesta chamada (saída estruturada pedida)")

    @staticmethod
    def _drop_reasoning_disable(
        kwargs: dict[str, Any], model: str, exc: Exception, teto: int | None = None
    ) -> bool:
        """O modelo exige raciocínio: tira o desligamento que NÓS pusemos e lembra.

        Devolve True quando tirou (e vale repetir). Só mexe no campo exato de
        ``_REASONING_DISABLED`` — um ``reasoning`` que o chamador mandou é dele.
        Com ``teto``, troca o desligamento pelo raciocínio limitado.
        """
        extra = kwargs.get("extra_body") or {}
        if extra.get("reasoning") != _REASONING_DISABLED["reasoning"]:
            return False
        if not _is_reasoning_mandatory(exc):
            return False
        _REASONING_MANDATORY.add(model)
        sem_campo = {k: v for k, v in extra.items() if k != "reasoning"}
        kwargs["extra_body"] = {**sem_campo, **_raciocinio_com_teto(teto)} if teto else sem_campo
        logger.warning(
            "[OpenRouter] %s exige raciocínio (não aceita reasoning.enabled=false) — "
            "refazendo com ele ligado%s, e assim fica para as próximas chamadas "
            "(conhecido de antemão se estiver em [openrouter] raciocinio_obrigatorio)",
            model,
            f" e limitado a {teto} tokens" if teto else "",
        )
        return True

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any,
    ):
        """Completion via OpenRouter (usa mesma lógica da OpenAI)."""
        teto = kwargs.pop("teto_raciocinio", None)
        model = self._split_provider_pin(model or self.DEFAULT_MODEL, kwargs)
        self._apply_provider_ignore(kwargs)
        self._apply_reasoning_pref(kwargs, model, teto)
        try:
            return await super().complete(
                messages=messages,
                tools=tools,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                # Com o pin trancado, o 429 tem que voltar NA HORA: quem conserta é
                # o `_relax_provider_pin` no except, e ele estava sendo chamado
                # depois de 17s de sono da escada de cima — o conserto barato
                # esperando o caro fracassar. Medido na session-d94dc42f: a resposta
                # final levou 36,8s, dos quais ~30s foram backoff contra um pool
                # lotado; solto o pin, a mesma chamada passou de primeira. Sem pin
                # trancado a flag não vai (aí o backoff é a única saída mesmo).
                bail_on_rate_limit=self._pin_is_locked(kwargs),
                **kwargs,
            )
        except LLMProviderError as e:
            if self._drop_reasoning_disable(kwargs, model, e, teto):
                # Reentra pelo caminho inteiro (pin, escada de 429): o modelo já
                # está limpo de `@slug` e o campo já saiu, então não há laço.
                return await self.complete(
                    messages=messages,
                    tools=tools,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs,
                )
            if self._relax_provider_pin(kwargs, e):
                # `False` explícito: agora o fallback está liberado, então esperar
                # volta a fazer sentido — a escada completa de 429 é a rede desta
                # segunda tentativa, e não existe terceira.
                return await super().complete(
                    bail_on_rate_limit=False,
                    messages=messages,
                    tools=tools,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs,
                )
            raise LLMProviderError(
                str(e.message).replace("OpenAI", "OpenRouter"),
                details={"provider": "openrouter", "model": model},
            ) from e

    async def stream_complete(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
        temperature: float = 0.9,
        max_tokens: int = 4096,
        **kwargs: Any,
    ):
        """Streaming via OpenRouter."""
        teto = kwargs.pop("teto_raciocinio", None)
        model = self._split_provider_pin(model or self.DEFAULT_MODEL, kwargs)
        self._apply_provider_ignore(kwargs)
        self._apply_reasoning_pref(kwargs, model, teto)
        # Até 3 voltas: uma para tirar o desligamento de raciocínio que o modelo
        # recusa, uma para afrouxar o pin lotado — cada conserto vale uma vez só.
        for tentativa in range(3):
            emitiu = False
            try:
                async for chunk in super().stream_complete(
                    messages=messages,
                    tools=tools,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs,
                ):
                    # O ERROR que o provider base emite antes de levantar NÃO é
                    # saída: contá-lo marcava `emitiu` em toda falha e a segunda
                    # tentativa nunca rodava — nem num 429 de livro. A exceção
                    # chega logo atrás e o `except` emite o ERROR se for desistir.
                    if chunk.event == StreamEventType.ERROR:
                        continue
                    emitiu = True
                    yield chunk
                return
            except LLMProviderError as e:
                # Refazer só vale se NADA saiu ainda: repetir depois de ter
                # mandado texto duplicaria a resposta na tela do usuário, e um
                # provedor lotado costuma recusar ANTES do primeiro chunk.
                if tentativa < 2 and not emitiu and (
                    self._drop_reasoning_disable(kwargs, model, e, teto)
                    or self._relax_provider_pin(kwargs, e)
                ):
                    continue
                yield StreamChunk(event=StreamEventType.ERROR, content=str(e))
                raise LLMProviderError(
                    str(e.message).replace("OpenAI", "OpenRouter"),
                    details={"provider": "openrouter", "model": model},
                ) from e
