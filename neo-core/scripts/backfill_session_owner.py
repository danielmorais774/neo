"""Preenche ``conversation_session.user_id`` onde ele é NULL.

**Por que existe** (docs/api-auth-plan.md F4). O escopo por dono das rotas de
sessão deixa passar sessão com ``user_id`` NULL de propósito: a coluna é anulável
e há conversa anterior a qualquer auth — filtrá-las faria o histórico
desaparecer do seletor de sessões, que é pior que o risco real. Este script tira
a exceção do caminho: cada sessão órfã herda o dono das próprias MENSAGENS
(``conversation_message.user_id``), e o que sobrar pode receber ``--dono <uuid>``.

    docker compose exec neo-core python3 scripts/backfill_session_owner.py --dry-run
    docker compose exec neo-core python3 scripts/backfill_session_owner.py
    docker compose exec neo-core python3 scripts/backfill_session_owner.py --dono <uuid>
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from db.async_client import AsyncPostgresClient  # noqa: E402
from shared.config import get_settings  # noqa: E402


async def main() -> int:
    seco = "--dry-run" in sys.argv
    dono_forcado = None
    if "--dono" in sys.argv:
        dono_forcado = sys.argv[sys.argv.index("--dono") + 1]

    db = AsyncPostgresClient(get_settings().database_url)

    async with db.get_session() as s:
        orfas = (
            await s.execute(
                text("SELECT count(*) FROM conversation_session WHERE user_id IS NULL")
            )
        ).scalar_one()
        print(f"sessões sem dono: {orfas}")

        # 1. Herdar das mensagens: o dono da conversa é quem escreveu nela.
        herda = text(
            """
            UPDATE conversation_session s
               SET user_id = m.user_id
              FROM (
                    SELECT session_id, min(user_id::text)::uuid AS user_id
                      FROM conversation_message
                     WHERE user_id IS NOT NULL
                  GROUP BY session_id
                    HAVING count(DISTINCT user_id) = 1
                   ) m
             WHERE s.id = m.session_id
               AND s.user_id IS NULL
            """
        )
        if seco:
            quantas = (
                await s.execute(
                    text(
                        """
                        SELECT count(*) FROM conversation_session s
                         WHERE s.user_id IS NULL
                           AND EXISTS (SELECT 1 FROM conversation_message m
                                        WHERE m.session_id = s.id AND m.user_id IS NOT NULL)
                        """
                    )
                )
            ).scalar_one()
            print(f"[dry-run] herdariam dono das mensagens: {quantas}")
        else:
            r = await s.execute(herda)
            print(f"herdaram dono das mensagens: {r.rowcount}")

        # 2. O resto (sessão vazia, ou com mensagens sem dono) só com --dono.
        if dono_forcado:
            if seco:
                print(f"[dry-run] o restante receberia {dono_forcado}")
            else:
                r = await s.execute(
                    text("UPDATE conversation_session SET user_id = :d WHERE user_id IS NULL"),
                    {"d": dono_forcado},
                )
                print(f"receberam o dono informado: {r.rowcount}")

        if not seco:
            await s.commit()

        restam = (
            await s.execute(
                text("SELECT count(*) FROM conversation_session WHERE user_id IS NULL")
            )
        ).scalar_one()
        print(f"ainda sem dono: {restam}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
