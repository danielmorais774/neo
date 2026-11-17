"""Cofre de credenciais do Neo — store Fernet + guard do PKS.

⚠️ **Este ``__init__`` é deliberadamente leve.** O ``guard`` é importado pelo
``core/pks/writer.py``, que é carregado SEMPRE; já o ``store`` depende de
``cryptography``. Se este módulo importasse o store no topo, um ambiente sem a
lib instalada quebraria o import do PKS inteiro — e, com o Mutagen + ``--reload``
do neo-core, isso derruba a produção antes de qualquer deploy (foi exatamente o
que o ``python-multipart`` fez em 2026-07-16; ver a memória de topologia).

Então: guard no topo (puro), store por trás de ``build_credential_store()``, que
só é chamado quando ``NEO_CRED_KEY`` está configurada.
"""

from __future__ import annotations

from typing import Any

from core.credentials.guard import (
    credential_violation,
    has_secret_material,
    is_secret_key,
    looks_like_secret_value,
)

__all__ = [
    "credential_violation",
    "has_secret_material",
    "is_secret_key",
    "looks_like_secret_value",
    "build_credential_store",
    "generate_credential_key",
]


def build_credential_store(*, db: Any, key: str) -> Any:
    """Monta o CredentialStore (importa ``cryptography`` só aqui).

    Devolve None se não houver chave — o broker então recusa ``fernet://`` com
    uma mensagem clara, em vez de o app inteiro morrer no import.
    """
    if not key:
        return None
    from core.credentials.store import CredentialStore

    return CredentialStore(db=db, key=key)


def generate_credential_key() -> str:
    """Gera uma NEO_CRED_KEY nova (uso: operador, via script/console)."""
    from core.credentials.store import generate_key

    return generate_key()
