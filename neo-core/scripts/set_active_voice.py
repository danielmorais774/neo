"""Seta manualmente a voz/engine ativa do TTS (sem passar pelo LLM).

Reusa a camada genérica de config (config_set / setting="tts.voice"): resolve o
nome contra o catálogo do neo-voice e persiste em user_preference
(service="neo-voice", category="tts", key="active").

Por padrão grava para TODOS os usuários existentes + global (user_id=NULL), já
que a voz ativa é lida pelo user_id da requisição do desktop.

Uso:
  python scripts/set_active_voice.py --name "JARVIS 4"
  python scripts/set_active_voice.py --name Ryan
  python scripts/set_active_voice.py --name "JARVIS 4" --user-id <uuid>

Requer o Postgres de pé (docker compose up -d neo-postgres) e o neo-voice acessível.
"""

from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from sqlalchemy import text

from db.async_client import AsyncPostgresClient
from modules.config import ConfigExecutor, build_default_registry


async def main() -> None:
    ap = argparse.ArgumentParser(description="Seta a voz ativa do TTS no banco.")
    ap.add_argument("--name", required=True, help="Nome da voz (ex: 'JARVIS 4', 'Ryan').")
    ap.add_argument("--engine", default=None,
                    help="(legado) ignorado — o engine vem do match no catálogo.")
    ap.add_argument("--user-id", default=None, help="UUID do usuário; default: todos + global.")
    args = ap.parse_args()
    if args.engine:
        print("[aviso] --engine é legado e está sendo ignorado (o engine vem do match).")

    db = AsyncPostgresClient()
    ex = ConfigExecutor(db=db, registry=build_default_registry())

    if args.user_id:
        targets: list[UUID | None] = [UUID(args.user_id)]
    else:
        async with db.get_session() as s:
            rows = (await s.execute(text('select id from "user"'))).all()
        targets = [r[0] for r in rows] + [None]  # cada usuário + global

    ok = False
    for uid in targets:
        ex.set_context(uid)
        out = await ex.execute("config_set", {"setting": "tts.voice", "value": args.name})
        status = "OK " if out.get("ok") else "FALHOU"
        print(f"[{status}] user={uid}: {out}")
        ok = ok or bool(out.get("ok"))

    if not ok:
        raise SystemExit("Nenhuma voz casou — confira o nome com /voices.")


if __name__ == "__main__":
    asyncio.run(main())
