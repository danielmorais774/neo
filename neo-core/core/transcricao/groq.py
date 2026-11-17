"""Cliente do Groq para transcrição (API compatível com a da OpenAI).

Duas chamadas: a lista de modelos (o painel escolhe o Whisper, e a gravação da
chave usa a lista como teste, porque ela não gasta cota) e a transcrição de um
arquivo de áudio, em ``verbose_json``, para ter os tempos de cada trecho.

A chave nunca vai para log nem para mensagem de erro.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.groq.com/openai/v1"


class GroqErro(Exception):
    """Falha de uma chamada ao Groq.

    ``tipo``: ``chave`` (recusada), ``limite`` (429), ``arquivo`` (grande demais ou
    formato), ``modelo`` (não existe), ``pedido`` (outro 4xx), ``servidor`` (5xx) ou
    ``rede`` (não chegou lá).
    """

    def __init__(
        self,
        tipo: str,
        mensagem: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        cabecalhos: dict[str, str] | None = None,
    ) -> None:
        super().__init__(mensagem)
        self.tipo = tipo
        self.mensagem = mensagem
        self.status = status
        self.retry_after = retry_after
        self.cabecalhos = cabecalhos or {}


@dataclass
class Resposta:
    dados: dict[str, Any]
    cabecalhos: dict[str, str] = field(default_factory=dict)


def _cabecalhos(resp: httpx.Response) -> dict[str, str]:
    return {k.lower(): v for k, v in resp.headers.items() if k.lower().startswith(("x-ratelimit", "retry-after"))}


def _mensagem_do_corpo(resp: httpx.Response) -> str:
    try:
        corpo = resp.json()
    except ValueError:
        return (resp.text or "").strip()[:300]
    erro = corpo.get("error") if isinstance(corpo, dict) else None
    if isinstance(erro, dict):
        return str(erro.get("message") or erro.get("code") or "").strip()[:300]
    return str(erro or corpo)[:300]


def _erro(resp: httpx.Response) -> GroqErro:
    status = resp.status_code
    msg = _mensagem_do_corpo(resp)
    cab = _cabecalhos(resp)
    if status in (401, 403):
        return GroqErro("chave", f"o Groq recusou a chave (HTTP {status}): {msg}", status=status, cabecalhos=cab)
    if status == 429:
        try:
            espera = float(resp.headers.get("retry-after") or "")
        except ValueError:
            espera = None
        return GroqErro("limite", f"limite do Groq atingido (HTTP 429): {msg}", status=status, retry_after=espera, cabecalhos=cab)
    if status == 413:
        return GroqErro("arquivo", f"arquivo grande demais para o Groq (HTTP 413): {msg}", status=status, cabecalhos=cab)
    if status == 404 or (status == 400 and "model" in msg.lower()):
        return GroqErro("modelo", f"o Groq não reconheceu o modelo (HTTP {status}): {msg}", status=status, cabecalhos=cab)
    if 400 <= status < 500:
        return GroqErro("pedido", f"o Groq recusou o pedido (HTTP {status}): {msg}", status=status, cabecalhos=cab)
    return GroqErro("servidor", f"o Groq falhou (HTTP {status}): {msg}", status=status, cabecalhos=cab)


async def listar_modelos(
    chave: str, *, timeout: float = 15.0, transport: httpx.AsyncBaseTransport | None = None
) -> list[dict[str, Any]]:
    """Os modelos de transcrição da conta (id com "whisper"), ativos primeiro."""
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout, transport=transport) as cliente:
            resp = await cliente.get("/models", headers={"Authorization": f"Bearer {chave}"})
    except httpx.HTTPError as e:
        raise GroqErro("rede", f"não consegui falar com o Groq: {type(e).__name__}") from e
    if resp.status_code != 200:
        raise _erro(resp)
    try:
        itens = resp.json().get("data") or []
    except ValueError as e:
        raise GroqErro("servidor", "o Groq devolveu uma lista de modelos ilegível") from e
    modelos = [
        {
            "id": str(m.get("id")),
            "dono": str(m.get("owned_by") or ""),
            "ativo": m.get("active") is not False,
        }
        for m in itens
        if isinstance(m, dict) and "whisper" in str(m.get("id") or "").lower()
    ]
    return sorted(modelos, key=lambda m: (not m["ativo"], m["id"]))


async def transcrever(
    chave: str,
    *,
    dados: bytes,
    nome: str,
    modelo: str,
    idioma: str | None = None,
    prompt: str | None = None,
    timeout: float = 120.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Resposta:
    campos: dict[str, str] = {"model": modelo, "response_format": "verbose_json", "temperature": "0"}
    if idioma:
        campos["language"] = idioma
    if prompt:
        campos["prompt"] = prompt
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout, transport=transport) as cliente:
            resp = await cliente.post(
                "/audio/transcriptions",
                headers={"Authorization": f"Bearer {chave}"},
                data=campos,
                files={"file": (nome, dados, "audio/ogg")},
            )
    except httpx.HTTPError as e:
        raise GroqErro("rede", f"não consegui falar com o Groq: {type(e).__name__}") from e
    if resp.status_code != 200:
        raise _erro(resp)
    try:
        corpo = resp.json()
    except ValueError as e:
        raise GroqErro("servidor", "o Groq devolveu uma transcrição ilegível") from e
    if not isinstance(corpo, dict):
        raise GroqErro("servidor", "o Groq devolveu uma transcrição em formato inesperado")
    return Resposta(dados=corpo, cabecalhos=_cabecalhos(resp))
