"""Classificação de sensibilidade de um campo do PKS (F2 do release-request-plan).

O problema medido em 2026-09-22: os cinco níveis existem
(``core/pks/ontology.py``) e o ``ContextBuilder`` já sabe esconder ``secret``,
mas **todo ``sensitivity=`` do código grava ``private``** — e ``private`` entra
no prompt. Resultado: no dia em que um CPF ou um CEP for gravado, ele passa a
viajar inteiro em todo turno que recuperar a entidade, antes de existir qualquer
pedido de liberação.

A classificação é por CHAVE, e vale na LEITURA. Assim ela cobre as linhas que já
estão no banco (todas ``private``) sem migração destrutiva, e cobre as futuras
mesmo que quem escreveu tenha esquecido de rotular. O nível guardado no banco
continua valendo quando é MAIS restritivo — quem marcou ``secret`` na mão não é
rebaixado por um regex.

O que isto NÃO faz: olhar o VALOR. Um CPF escrito dentro de um campo
``description`` não é pego aqui — texto livre é problema de outra camada
(§7 do plano). Esta é a fronteira barata e determinística: nome do campo.

Estado do PKS quando isto foi escrito (contagem real, sem valores): 102 chaves
distintas, e de identificação só ``email`` (2) e ``cnpj`` (2), mais resíduo
financeiro (``balance``, ``amount``). Ou seja: isto é preventivo, e o custo de
ligar hoje é quase zero.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

NORMAL = "normal"
SENSITIVE = "sensitive"
SECRET = "secret"

#: Quanto maior, mais restritivo. ``private``/``internal``/``public`` do
#: ontology são todos "aparece no prompt" — entram como NORMAL.
_ORDER = {"public": 0, "internal": 0, "private": 0, NORMAL: 0, SENSITIVE: 1, SECRET: 2}

#: Chaves que parecem sensíveis mas NÃO são (e que o Neo usa todo dia).
#: ``birthday`` já sai truncado em dia/mês pelo ContextBuilder — é o que faz o
#: "amanhã é aniversário do fulano" funcionar sem a data inteira.
_EXCECOES = frozenset({"birthday", "name", "full_name", "preferred_name", "nome"})

_SECRET_RE = re.compile(
    r"(senha|password|passwd|\bpwd\b|token|api_?key|secret|chave_api|chave_pix|"
    r"\bcvv\b|\bcvc\b|\botp\b|2fa|seed_phrase|private_key)",
    re.IGNORECASE,
)

_SENSITIVE_RE = re.compile(
    # documento
    r"(\bcpf\b|\bcnpj\b|\brg\b|\bcnh\b|passaporte|passport|titulo_eleitor|"
    r"\bpis\b|\bnit\b|inscricao_estadual|\bssn\b|tax_id|"
    # endereço
    r"endereco|endereço|address|\bcep\b|zip_?code|logradouro|bairro|complemento|"
    # contato
    r"e_?mail|telefone|celular|\bphone\b|whatsapp|\bfone\b|"
    # financeiro (o PKS não é extrato — ver core/pks/ledger_guard)
    r"saldo|balance|amount|\biban\b|\bpix\b|agencia|conta_bancaria|conta_corrente|"
    r"numero_conta|cartao|cartão|card_number|fatura|limite_credito|"
    # nascimento (o dia/mês continua pelo `birthday`)
    r"nascimento|birth_?date|data_nasc)",
    re.IGNORECASE,
)


def classify_key(key: str) -> str:
    """Nível que o NOME do campo pede, ignorando o que está gravado."""
    nome = (key or "").strip()
    if not nome or nome.lower() in _EXCECOES:
        return NORMAL
    if _SECRET_RE.search(nome):
        return SECRET
    if _SENSITIVE_RE.search(nome):
        return SENSITIVE
    return NORMAL


def effective_level(key: str, stored: str | None = None) -> str:
    """O mais restritivo entre o que está gravado e o que a chave pede."""
    pela_chave = classify_key(key)
    gravado = (stored or NORMAL).strip().lower()
    return pela_chave if _ORDER.get(pela_chave, 0) >= _ORDER.get(gravado, 0) else gravado


def is_redacted(key: str, stored: str | None = None) -> bool:
    return effective_level(key, stored) in (SENSITIVE, SECRET)


def handle_for(key: str) -> str:
    """O que aparece NO LUGAR do valor. É um endereço, não o dado."""
    return f"«pks:{(key or '').strip()}»"


def redaction_enabled() -> bool:
    try:
        from shared.config import get_settings

        return bool(getattr(get_settings(), "neo_pks_redaction_enabled", True))
    except Exception:  # noqa: BLE001 — sem settings, o lado seguro é redigir
        return True


def redact_value(key: str, value: Any, stored: str | None = None) -> tuple[Any, bool]:
    """``(valor_ou_handle, foi_redigido)`` para um campo."""
    if not redaction_enabled():
        return value, False
    nivel = effective_level(key, stored)
    if nivel == NORMAL:
        return value, False
    return handle_for(key), True


def redact_properties(
    properties: dict[str, Any], sensitivities: dict[str, str] | None = None
) -> dict[str, Any]:
    """Versão do dict de campos pronta para ir ao prompt / ao resultado de tool.

    ``sensitivities`` é opcional: quando o chamador tem o nível gravado de cada
    campo, ele entra na conta; quando não tem, vale só a chave.
    """
    if not properties or not redaction_enabled():
        return properties
    saida: dict[str, Any] = {}
    redigidos: list[str] = []
    for chave, valor in properties.items():
        novo, foi = redact_value(chave, valor, (sensitivities or {}).get(chave))
        saida[chave] = novo
        if foi:
            redigidos.append(chave)
    if redigidos:
        logger.debug("[PKS][redação] campos fora do prompt: %s", ", ".join(redigidos))
    return saida
