"""Cliente MCP mínimo para o Zion, só leitura (docs/zion-integracao-plan.md §7.1).

O Zion fala MCP por HTTP ("Streamable HTTP" do SDK). O neo-core só precisa de
três mensagens, então o cliente é este arquivo em cima do ``httpx`` (que já está
na imagem), sem dependência nova:

1. ``initialize``, com ``clientInfo.name = "neo"``: é por esse nome que o Zion
   sabe que o agente é o Neo. A resposta traz o ``Mcp-Session-Id``.
2. ``notifications/initialized``.
3. ``tools/call``.

Medido na F0: as três respostas vieram em ``text/event-stream`` (o JSON-RPC
vem nas linhas ``data:``). O cliente aceita JSON também, que custa uma linha e
protege contra uma troca de versão do SDK do lado de lá.

O servidor guarda a sessão em memória. Se ele reiniciou (deploy do Zion), a
sessão antiga responde 404, e o cliente refaz o ``initialize`` uma vez.

Erro de rede, de acesso ou de protocolo vira :class:`ZionIndisponivel`, com a
frase pronta para o modelo ler. Quem chama transforma isso em resultado, nunca
em exceção que derruba o turno.
"""

from __future__ import annotations

import itertools
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: Versão do MCP pedida no ``initialize``. O servidor responde com a que vai
#: usar, e é essa que segue no cabeçalho das chamadas seguintes.
PROTOCOLO = "2025-06-18"

_ACEITA = "application/json, text/event-stream"


class ZionIndisponivel(Exception):
    """O Zion não respondeu, recusou o acesso ou respondeu algo que não é MCP."""


class ZionRecusou(Exception):
    """O Zion respondeu, mas recusou a chamada (argumento inválido, tool que não existe)."""


def mensagens_sse(texto: str) -> list[dict[str, Any]]:
    """Os objetos JSON de um corpo ``text/event-stream``.

    Um evento é um bloco de linhas terminado por linha em branco; as linhas
    ``data:`` dele se juntam com ``\\n`` (é a regra do SSE). Evento cujo dado não
    é JSON é ignorado.
    """
    mensagens: list[dict[str, Any]] = []
    dados: list[str] = []
    for linha in [*texto.splitlines(), ""]:
        if not linha.strip():
            if dados:
                try:
                    obj = json.loads("\n".join(dados))
                except ValueError:
                    obj = None
                if isinstance(obj, dict):
                    mensagens.append(obj)
                dados = []
            continue
        if linha.startswith("data:"):
            valor = linha[5:]
            dados.append(valor[1:] if valor.startswith(" ") else valor)
    return mensagens


def _resposta_rpc(resposta: httpx.Response, id_pedido: int) -> dict[str, Any]:
    """A mensagem JSON-RPC que responde ao pedido ``id_pedido``."""
    tipo = resposta.headers.get("content-type", "")
    if "text/event-stream" in tipo:
        candidatas = mensagens_sse(resposta.text)
    else:
        try:
            corpo = resposta.json()
        except ValueError as exc:
            raise ZionIndisponivel(
                f"resposta que não é JSON (HTTP {resposta.status_code}, {tipo or 'sem tipo'})"
            ) from exc
        candidatas = corpo if isinstance(corpo, list) else [corpo]
    for msg in candidatas:
        if isinstance(msg, dict) and msg.get("id") == id_pedido:
            return msg
    raise ZionIndisponivel(f"a resposta não trouxe o pedido {id_pedido} (HTTP {resposta.status_code})")


def resultado_da_tool(result: dict[str, Any]) -> Any:
    """O valor que uma tool do Zion devolveu.

    O Zion manda o mesmo JSON em ``structuredContent`` e como texto. Usa o
    estruturado; sem ele, o texto (como JSON, ou cru se não for JSON).
    """
    if "structuredContent" in result and result["structuredContent"] is not None:
        return result["structuredContent"]
    textos = [
        str(bloco.get("text") or "")
        for bloco in result.get("content") or []
        if isinstance(bloco, dict) and bloco.get("type") == "text"
    ]
    texto = "\n".join(textos)
    try:
        return json.loads(texto)
    except ValueError:
        return texto


class ClienteZion:
    """Uma sessão MCP com o Zion, reaproveitada entre chamadas.

    Sem trava: duas chamadas simultâneas sem sessão abrem duas, e a segunda
    vence. Custa uma sessão a mais no servidor, que é barata, e evita uma trava
    presa a um event loop.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self.timeout_s = timeout_s
        self._transport = transport
        self._sessao: str | None = None
        self._protocolo = PROTOCOLO
        self._ids = itertools.count(1)

    def _cabecalhos(self) -> dict[str, str]:
        cab = {"Accept": _ACEITA, "Content-Type": "application/json"}
        if self._sessao:
            cab["Mcp-Session-Id"] = self._sessao
            cab["Mcp-Protocol-Version"] = self._protocolo
        return cab

    async def _post(self, http: httpx.AsyncClient, corpo: dict[str, Any]) -> httpx.Response:
        try:
            return await http.post(self.url, json=corpo, headers=self._cabecalhos())
        except httpx.TimeoutException as exc:
            raise ZionIndisponivel(f"sem resposta em {self.timeout_s:g} s ({self.url})") from exc
        except httpx.HTTPError as exc:
            raise ZionIndisponivel(f"{type(exc).__name__} em {self.url}: {exc}") from exc

    @staticmethod
    def _conferir_status(resposta: httpx.Response) -> None:
        if resposta.status_code in (401, 403):
            raise ZionIndisponivel(
                f"o Zion recusou o acesso (HTTP {resposta.status_code}: "
                f"{resposta.text[:160].strip()})"
            )
        if resposta.status_code >= 400:
            raise ZionIndisponivel(f"HTTP {resposta.status_code}: {resposta.text[:160].strip()}")

    async def _inicializar(self, http: httpx.AsyncClient) -> None:
        self._sessao = None
        id_pedido = next(self._ids)
        resposta = await self._post(
            http,
            {
                "jsonrpc": "2.0",
                "id": id_pedido,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOLO,
                    "capabilities": {},
                    "clientInfo": {"name": "neo", "version": "1"},
                },
            },
        )
        self._conferir_status(resposta)
        msg = _resposta_rpc(resposta, id_pedido)
        if "error" in msg:
            raise ZionIndisponivel(f"initialize recusado: {msg['error']}")
        sessao = resposta.headers.get("mcp-session-id")
        if not sessao:
            raise ZionIndisponivel("o initialize não devolveu Mcp-Session-Id")
        self._sessao = sessao
        self._protocolo = str((msg.get("result") or {}).get("protocolVersion") or PROTOCOLO)
        aviso = await self._post(http, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        if aviso.status_code >= 400:
            self._sessao = None
            raise ZionIndisponivel(f"notifications/initialized deu HTTP {aviso.status_code}")
        logger.info("[zion] sessão MCP aberta (%s…, protocolo %s)", sessao[:8], self._protocolo)

    async def chamar(self, tool: str, argumentos: dict[str, Any]) -> Any:
        """Chama uma tool do Zion e devolve o valor que ela respondeu.

        Levanta :class:`ZionIndisponivel` (rede, acesso, protocolo) ou
        :class:`ZionRecusou` (o Zion recusou a chamada em si).
        """
        async with httpx.AsyncClient(timeout=self.timeout_s, transport=self._transport) as http:
            for tentativa in (1, 2):
                if self._sessao is None:
                    await self._inicializar(http)
                id_pedido = next(self._ids)
                resposta = await self._post(
                    http,
                    {
                        "jsonrpc": "2.0",
                        "id": id_pedido,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": argumentos},
                    },
                )
                # Sessão perdida (o Zion reiniciou): uma nova, uma vez só.
                if resposta.status_code == 404 and tentativa == 1:
                    logger.info("[zion] sessão MCP perdida (404); abrindo outra")
                    self._sessao = None
                    continue
                self._conferir_status(resposta)
                msg = _resposta_rpc(resposta, id_pedido)
                if "error" in msg:
                    erro = msg["error"] or {}
                    raise ZionRecusou(str(erro.get("message") or erro)[:400])
                result = msg.get("result") or {}
                if result.get("isError"):
                    raise ZionRecusou(str(resultado_da_tool({"content": result.get("content")}))[:400])
                return resultado_da_tool(result)
        raise ZionIndisponivel("a sessão MCP caiu duas vezes seguidas")  # pragma: no cover
