#!/usr/bin/env python3
"""Backfill dos embeddings das mensagens sem vetor (F0.3).

Todo ponto que salva uma resposta do Neo passava ``generate_embedding=False``,
então o banco tinha 0 de 1520 respostas embeddadas: a busca vetorial só
enxergava as perguntas do usuário e nunca achava o que o PRÓPRIO Neo disse.
Daqui em diante o `PersistentSessionManager` preenche em background; este
script fecha o passado.

    docker exec -w /app neo-core python scripts/backfill_assistant_embeddings.py --dry-run
    docker exec -w /app neo-core python scripts/backfill_assistant_embeddings.py

Idempotente: só toca em linhas com `embedding IS NULL`. Pode ser interrompido e
retomado — o que já foi gravado não é refeito. O modelo é local
(all-MiniLM-L6-v2, CPU), então não há custo de API.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

sys.path.insert(0, "/app")

from sqlalchemy import text  # noqa: E402

from core.session.embedding_service import EmbeddingService  # noqa: E402
from db.async_client import AsyncPostgresClient  # noqa: E402

BATCH = 100


async def _pending(db: AsyncPostgresClient, role: str | None) -> int:
    role_filter = "AND role = :role" if role else ""
    async with db.get_session() as session:
        res = await session.execute(
            text(
                "SELECT count(*) FROM conversation_message "
                f"WHERE embedding IS NULL AND length(btrim(content)) > 0 {role_filter}"
            ),
            {"role": role} if role else {},
        )
        return int(res.scalar() or 0)


async def _next_batch(db: AsyncPostgresClient, role: str | None) -> list[tuple]:
    role_filter = "AND role = :role" if role else ""
    async with db.get_session() as session:
        res = await session.execute(
            text(
                "SELECT id, content FROM conversation_message "
                f"WHERE embedding IS NULL AND length(btrim(content)) > 0 {role_filter} "
                "ORDER BY created_at DESC LIMIT :lim"
            ),
            {"lim": BATCH, **({"role": role} if role else {})},
        )
        return list(res.fetchall())


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Só conta o que falta")
    parser.add_argument(
        "--role", default="assistant",
        help="Papel a preencher (default: assistant; 'all' para todos)",
    )
    args = parser.parse_args()
    role = None if args.role == "all" else args.role

    db = AsyncPostgresClient()
    try:
        total = await _pending(db, role)
        print(f"mensagens sem embedding (role={args.role}): {total}")
        if args.dry_run or total == 0:
            return 0

        embeddings = EmbeddingService()
        if not embeddings.available:
            print("EmbeddingService indisponível — nada a fazer")
            return 1

        done = 0
        failed = 0
        t0 = time.monotonic()
        while True:
            rows = await _next_batch(db, role)
            if not rows:
                break
            try:
                vectors = await embeddings.embed_batch([str(r[1]) for r in rows])
            except Exception as e:  # noqa: BLE001
                print(f"lote falhou ({e}) — caindo para um a um")
                vectors = []
                for row in rows:
                    try:
                        vectors.append(await embeddings.embed(str(row[1])))
                    except Exception:  # noqa: BLE001
                        vectors.append(None)

            for row, vector in zip(rows, vectors):
                if vector is None:
                    failed += 1
                    continue
                await db.set_message_embedding(row[0], vector)
                done += 1

            elapsed = time.monotonic() - t0
            print(
                f"  {done}/{total} ({done / max(total, 1):.0%}) "
                f"— {done / max(elapsed, 0.001):.0f} msg/s",
                flush=True,
            )
            if failed and failed >= len(rows):
                print("lote inteiro falhou — abortando para não girar em falso")
                return 1

        restante = await _pending(db, role)
        print(f"\nfeito: {done} preenchidas, {failed} falharam, {restante} restantes")
        return 0
    finally:
        await db.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
