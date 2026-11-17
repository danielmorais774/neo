"""Define ou troca a senha de um usuário (docs/api-auth-plan.md §5).

A senha nunca chega por argumento de linha de comando — argumento fica no
histórico do shell e na lista de processos. É lida do terminal (sem eco) ou de
``NEO_NEW_PASSWORD`` para uso não interativo.

    docker compose exec neo-core python3 scripts/set_password.py daniel
    NEO_NEW_PASSWORD=... docker compose exec -T neo-core python3 scripts/set_password.py daniel
"""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.auth.passwords import hash_password  # noqa: E402
from db.async_client import AsyncPostgresClient  # noqa: E402
from shared.config import get_settings  # noqa: E402

MIN_LEN = 10


async def main() -> int:
    if len(sys.argv) < 2:
        print("uso: set_password.py <usuário>")
        return 2
    username = sys.argv[1].strip()

    senha = os.environ.get("NEO_NEW_PASSWORD")
    if senha is None:
        senha = getpass.getpass(f"Senha nova para {username}: ")
        confirmacao = getpass.getpass("Confirme: ")
        if senha != confirmacao:
            print("as senhas não batem")
            return 1

    if len(senha) < MIN_LEN:
        print(f"senha curta demais (mínimo {MIN_LEN} caracteres)")
        return 1

    db = AsyncPostgresClient(get_settings().database_url)
    user = await db.get_user_by_username(username)
    if user is None:
        print(f"usuário '{username}' não existe")
        return 1

    await db.set_user_password_hash(user.id, hash_password(senha))
    # Trocar senha invalida sessão: um refresh vivo emitido com a senha antiga
    # continuaria valendo, e "troquei a senha" não teria efeito nenhum.
    revogados = await db.revoke_all_refresh_tokens(user.id)
    print(f"senha de '{username}' atualizada (PBKDF2). Refresh tokens revogados: {revogados}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
