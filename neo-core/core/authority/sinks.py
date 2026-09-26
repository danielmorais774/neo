"""Qual argumento de cada saída é o DESTINO (F4 do release-request-plan).

O portão sabia dizer "isto é uma saída"; para casar com uma liberação ele precisa
saber **para onde** a saída vai — e isso não é genérico: em ``email_send`` o
destino é ``para``, em ``whatsapp_send_message`` é ``recipient``, em
``service_request`` é o slug do serviço, em ``web_fetch`` é o host da URL.

Sem este mapa, "aprovei mandar o CEP para a loja" não teria como ser comparado
com "o modelo está chamando email_send para outro@dominio.com". Com ele, a
liberação passa a ser por **(dado × destino)**, que é a granularidade que o plano
inteiro promete.

Tool cuja saída não tem destino endereçável (``run_command``, ``neo_bash``,
escrita de arquivo, ``pks_remember``) devolve ``None``: ela nunca é coberta por
liberação de destino, e continua decidida só pela origem do turno.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class Destination:
    """Para onde esta chamada entrega."""

    tipo: str  # email | whatsapp | dominio
    valor: str

    def __str__(self) -> str:  # pragma: no cover - conveniência de log
        return f"{self.valor} ({self.tipo})"


def _primeiro_endereco(bruto: Any) -> str:
    """``"a@x.com, b@y.com"`` → ``a@x.com``. Vários destinos = o primeiro manda.

    Quem aprova vê a lista inteira no card; aqui o que importa é ter UM valor
    comparável. Liberação para um endereço não cobre um segundo destinatário:
    o casamento exige igualdade, então a lista inteira precisa ser aprovada.
    """
    texto = str(bruto or "").strip()
    if not texto:
        return ""
    return texto.replace(";", ",").split(",")[0].strip().lower()


def _host(bruto: Any) -> str:
    texto = str(bruto or "").strip()
    if not texto:
        return ""
    try:
        return (urlparse(texto).hostname or "").lower().strip(".")
    except Exception:  # noqa: BLE001
        return ""


def destination_of(tool_name: str, arguments: dict[str, Any] | None) -> Destination | None:
    """O destino desta chamada, ou ``None`` quando não há um endereçável."""
    args = arguments or {}
    nome = (tool_name or "").strip()

    if nome == "email_send":
        endereco = _primeiro_endereco(args.get("para") or args.get("to"))
        return Destination("email", endereco) if endereco else None

    if nome == "whatsapp_send_message":
        # Sem `recipient` o envio vai para o contato da missão — destino real,
        # mas que este mapa não enxerga (mora no banco). Devolver None deixa a
        # decisão com a origem do turno, que é o lado seguro.
        alvo = str(args.get("recipient") or "").strip().lower()
        return Destination("whatsapp", alvo) if alvo else None

    if nome in ("service_request", "service_confirm"):
        # O host NUNCA vem do LLM no gateway (só o slug da conexão) — então o
        # slug É o destino comparável aqui.
        slug = str(args.get("service") or "").strip().lower()
        return Destination("dominio", slug) if slug else None

    if nome == "web_fetch":
        host = _host(args.get("url"))
        return Destination("dominio", host) if host else None

    if nome in ("browser_task", "browser_local_task"):
        # A tarefa é texto livre ("compra a cortina na loja X"): o site onde o
        # dado seria digitado só se sabe passo a passo. Quem guarda ali é o
        # preenchimento escopado (core/release/fill.py), que compara o domínio
        # da ABA. Aqui, destino não endereçável.
        return None

    return None


#: Qual argumento carrega o CONTEÚDO aprovado, por tool. É o que o
#: ``payload_hash`` da liberação amarra: aprovou aquele texto, sai aquele texto.
_PAYLOAD_ARG = {
    "email_send": "corpo",
    "whatsapp_send_message": "text",
}


def payload_of(tool_name: str, arguments: dict[str, Any] | None) -> Any:
    """O conteúdo desta chamada, quando ela entrega um texto redigido."""
    chave = _PAYLOAD_ARG.get((tool_name or "").strip())
    if not chave:
        return None
    valor = (arguments or {}).get(chave)
    return valor if valor not in (None, "") else None


def has_destination(tool_name: str) -> bool:
    """Esta tool é do tipo que tem destino endereçável (com argumentos certos)?"""
    return (tool_name or "").strip() in _COM_DESTINO


_COM_DESTINO = frozenset(
    {
        "email_send",
        "whatsapp_send_message",
        "service_request",
        "service_confirm",
        "web_fetch",
    }
)


def covers(release_destino: str, release_tipo: str, destino: Destination | None) -> bool:
    """A liberação cobre este destino?

    ``email``/``whatsapp`` exigem igualdade (endereço é endereço). ``dominio``
    aceita subdomínio, pela mesma regra do preenchimento escopado — e pelo mesmo
    motivo: ``loja.com.br`` não pode cobrir ``loja.com.br.atacante.tld``.
    """
    if destino is None:
        return False
    alvo = (release_destino or "").strip().lower()
    if not alvo:
        return False
    if (release_tipo or "").strip().lower() != destino.tipo:
        return False
    if destino.tipo == "dominio":
        from core.release.fill import domain_matches

        return domain_matches(alvo, destino.valor)
    return alvo == destino.valor
