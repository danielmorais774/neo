"""Indexa em PEDAÇOS as mensagens longas que já estão no banco.

O embedder local (`all-MiniLM-L6-v2`) trunca em 256 tokens sem avisar: o vetor
de uma mensagem longa representa só a abertura dela. Tudo que foi escrito antes
do chunking existir está com esse ponto cego — inclusive as análises que o
usuário pede de volta. Ver ConversationMessageChunk.

    docker exec neo-core python3 scripts/backfill_message_chunks.py [--dry-run]
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from core.memory.chunking import chunk_text  # noqa: E402
from core.session.embedding_service import EmbeddingService  # noqa: E402
from db.async_client import AsyncPostgresClient  # noqa: E402
from shared.config import get_settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backfill-chunks")


async def main(dry_run: bool = False) -> None:
    st = get_settings()
    minimo = getattr(st, "embedding_chunk_min_chars", 1000)
    size = getattr(st, "embedding_chunk_chars", 500)
    overlap = getattr(st, "embedding_chunk_overlap", 250)
    # Override por linha de comando: usado pela calibração, que precisa
    # reindexar o corpus inteiro numa config candidata para comparar ranks de
    # forma justa (com os RIVAIS também fatiados).
    for arg in sys.argv[1:]:
        if arg.startswith("--size="):
            size = int(arg.split("=", 1)[1])
        elif arg.startswith("--overlap="):
            overlap = int(arg.split("=", 1)[1])

    db = AsyncPostgresClient()
    embeddings = EmbeddingService()
    if not embeddings.available:
        logger.error("EmbeddingService indisponível — nada a fazer.")
        return

    async with db.get_session() as s:
        reindex = "" if "--force" in sys.argv else "AND c.id IS NULL"
        res = await s.execute(
            text(f"""
                SELECT cm.id, cm.session_id, cm.user_id, cm.content
                FROM conversation_message cm
                LEFT JOIN conversation_message_chunk c ON c.message_id = cm.id
                WHERE cm.role IN ('user', 'assistant')
                  AND cm.tool_name IS NULL
                  AND length(cm.content) > :minimo
                  {reindex}
                ORDER BY length(cm.content) DESC
            """),
            {"minimo": minimo},
        )
        alvos = [dict(r._mapping) for r in res.fetchall()]

    logger.info("%s mensagens longas sem pedaços indexados.", len(alvos))
    if dry_run:
        for a in alvos[:10]:
            logger.info("  %s · %s chars → %s pedaços",
                        str(a["id"])[:8], len(a["content"]),
                        len(chunk_text(a["content"], size, overlap)))
        return

    total_pedacos = 0
    for i, a in enumerate(alvos, 1):
        pedacos = chunk_text(a["content"], size, overlap)
        if len(pedacos) < 2:
            continue
        try:
            vetores = await embeddings.embed_batch(pedacos)
            pares = [(p, v) for p, v in zip(pedacos, vetores) if v is not None]
            n = await db.replace_message_chunks(
                a["id"], a["session_id"], a["user_id"], pares
            )
            total_pedacos += n
            if i % 20 == 0 or i == len(alvos):
                logger.info("  %s/%s mensagens · %s pedaços",
                            i, len(alvos), total_pedacos)
        except Exception as e:  # noqa: BLE001
            logger.warning("  falhou em %s: %s", str(a["id"])[:8], e)

    logger.info("Pronto: %s pedaços de %s mensagens.", total_pedacos, len(alvos))


if __name__ == "__main__":
    asyncio.run(main(dry_run="--dry-run" in sys.argv))
