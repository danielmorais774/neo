#!/usr/bin/env python
"""Inspeciona workspaces e entidades pessoais de usuários (read-only).

Invocado pelo comando /inspect-user via:
    DOCKER_HOST=ssh://neo-vagrant docker exec neo-core python /app/.claude/commands/scripts/inspect_user.py [args]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app")

from sqlalchemy import text

from core.pks.repository import PKSRepository
from db.async_client import AsyncPostgresClient
from shared.config import get_settings

_DURATION = re.compile(r"^(\d+)([smhd])$")


def _parse_since(value: str) -> datetime:
    m = _DURATION.match(value.strip().lower())
    if not m:
        raise SystemExit(f"--since espera ex: '24h'; recebeu {value!r}")
    qty, unit = int(m.group(1)), m.group(2)
    delta = {"s": timedelta(seconds=qty), "m": timedelta(minutes=qty),
             "h": timedelta(hours=qty), "d": timedelta(days=qty)}[unit]
    return datetime.now(timezone.utc) - delta


def _summarize_props(props) -> dict:
    if not isinstance(props, dict):
        return {}
    out = {}
    for k, v in props.items():
        if isinstance(v, list):
            out[k] = f"list[{len(v)}]"
        elif isinstance(v, dict):
            out[k] = f"dict({len(v)} keys)"
        else:
            out[k] = v
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("workspace", nargs="?",
                    help="nome ou UUID do workspace (omita para --list)")
    ap.add_argument("--list", action="store_true", help="listar todos os workspaces")
    ap.add_argument("--entity", default=None, help="dump JSON completo desta entidade")
    ap.add_argument("--type", default=None, help="filtrar por tipo")
    ap.add_argument("--since", default=None, help="ex: 24h, 30m, 2d")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    db = AsyncPostgresClient(get_settings().database_url)
    repo = PKSRepository(db.session_factory)
    try:
        async with db.get_session() as session:
            if args.list or not args.workspace:
                rows = (await session.execute(
                    text("SELECT id, name, type FROM pks_workspace "
                         "WHERE deleted_at IS NULL ORDER BY type, name")
                )).mappings().all()
                print(f"{len(rows)} workspace(s):\n")
                for r in rows:
                    print(f"  [{r['type']:10}] {r['name']:40} {r['id']}")
                return

            ws_q = await session.execute(
                text("SELECT id, name, type FROM pks_workspace "
                     "WHERE deleted_at IS NULL AND lower(name)=lower(:q) "
                     "ORDER BY CASE WHEN type='system' THEN 0 ELSE 1 END LIMIT 1"),
                {"q": args.workspace},
            )
            ws = ws_q.mappings().first()
            if not ws:
                try:
                    ws_q2 = await session.execute(
                        text("SELECT id, name, type FROM pks_workspace "
                             "WHERE id=CAST(:id AS uuid) AND deleted_at IS NULL"),
                        {"id": args.workspace},
                    )
                    ws = ws_q2.mappings().first()
                except Exception:
                    pass
            if not ws:
                print(f"workspace não encontrado: {args.workspace!r}", file=sys.stderr)
                sys.exit(2)
            wsid = ws["id"]
            print(f"workspace: {ws['name']} [{ws['type']}] ({wsid})\n")

            sql = ("SELECT id, name, type, created_at, updated_at "
                   "FROM pks_entity WHERE workspace_id=:w AND deleted_at IS NULL")
            params: dict = {"w": wsid}
            if args.type:
                sql += " AND type=:t"; params["t"] = args.type
            if args.since:
                sql += " AND updated_at>=:since"; params["since"] = _parse_since(args.since)
            sql += " ORDER BY updated_at DESC, type, name LIMIT :lim"
            params["lim"] = args.limit
            rows = (await session.execute(text(sql), params)).mappings().all()
            print(f"{len(rows)} entidade(s):\n")

            dump_target = args.entity.lower() if args.entity else None
            dump_payload = None
            for ent in rows:
                field = await repo.get_field(session, ent["id"], "properties")
                props = field.value if field and isinstance(field.value, dict) else None
                summary = _summarize_props(props)
                cr = ent["created_at"].strftime("%Y-%m-%d %H:%M:%S")
                up = ent["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
                print(f"- {ent['name']} [{ent['type']}]")
                print(f"    id={ent['id']}")
                print(f"    created={cr}  updated={up}")
                if summary:
                    print(f"    props: {json.dumps(summary, ensure_ascii=False)[:260]}")
                print()
                if dump_target and ent["name"].lower() == dump_target:
                    dump_payload = props
            if dump_target:
                if dump_payload is None:
                    print(f"(entidade {args.entity!r} não encontrada)")
                else:
                    print(f"=== {args.entity} — props completas ===")
                    print(json.dumps(dump_payload, ensure_ascii=False, indent=2))
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
