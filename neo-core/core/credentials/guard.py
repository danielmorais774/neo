"""Guard: material de credencial NUNCA vira memória semântica.

Mesmo padrão (e mesma motivação) do ``core/pks/ledger_guard.py``: lá, dinheiro
não entra no PKS porque o planner inventava saldo fantasma copiando o extrato;
aqui, segredo não entra no PKS porque (a) a fonte da verdade é o cofre cifrado, e
(b) o PKS é injetado no prompt — um token que caia numa entidade vaza para o
contexto do LLM em todo turno seguinte, que é exatamente o que o gateway inteiro
existe para evitar.

Função pura + enforcement no ``pks/writer.py`` (como o ledger). O que PODE virar
conhecimento é o fato ("o serviço X usa a credencial 'github-personal'"), nunca
o valor.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

# Chaves de campo que denunciam segredo ("api_key", "senha", "token de acesso"…).
_SECRET_KEY = re.compile(
    r"\b("
    r"token|api[\s_-]?key|apikey|secret|senha|password|passwd|pwd|"
    r"credential|credencial|bearer|authorization|cookie|"
    r"private[\s_-]?key|chave[\s_-]?privada|client[\s_-]?secret|"
    r"access[\s_-]?key|refresh[\s_-]?token"
    r")\b"
)

# Valores que SÃO segredo, mesmo que a chave pareça inocente (o planner adora
# um "detalhe: ghp_...."). Espelha os padrões do gateway/redact.py.
_SECRET_VALUE = re.compile(
    r"(gh[posru]_[A-Za-z0-9]{20,})"
    r"|(xox[baprs]-[A-Za-z0-9-]{10,})"
    r"|(sk-[A-Za-z0-9]{20,})"
    r"|(AKIA[0-9A-Z]{16})"
    r"|(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,})"
    r"|(-----BEGIN [A-Z ]*PRIVATE KEY-----)"
    r"|((?i:bearer)\s+[A-Za-z0-9._\-~+/]{16,})"
)


def _norm(text: str) -> str:
    """lower + sem acentos, para casar 'Senha' e 'credencial'."""
    normalized = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in normalized if not unicodedata.combining(c)).lower().strip()


def is_secret_key(key: str | None) -> bool:
    return bool(key and _SECRET_KEY.search(_norm(str(key))))


def looks_like_secret_value(value: Any) -> bool:
    if not isinstance(value, str) or len(value) < 12:
        return False
    return bool(_SECRET_VALUE.search(value))


def has_secret_material(keys: Iterable[str] = (), values: Iterable[Any] = ()) -> bool:
    """Conveniência para checar um dict de campos de uma vez."""
    return any(is_secret_key(k) for k in keys) or any(looks_like_secret_value(v) for v in values)


def credential_violation(op: Any) -> str | None:
    """Motivo pelo qual esta operação de PKS carrega segredo — ou None.

    Recebe uma ``PKSOperation`` (``entity``/``field`` são dicts do planner),
    igual ao ``ledger_violation``.
    """
    field = getattr(op, "field", None) or {}

    key = field.get("key")
    if is_secret_key(key):
        return f"credential_field:{key}"

    for candidate in (field.get("value_text"), field.get("value_json")):
        if looks_like_secret_value(candidate):
            return "credential_value"
        # value_json pode ser dict/list — varre um nível raso.
        if isinstance(candidate, dict):
            if has_secret_material(candidate.keys(), candidate.values()):
                return "credential_value_json"
        elif isinstance(candidate, list):
            if any(looks_like_secret_value(v) for v in candidate):
                return "credential_value_json"

    return None
