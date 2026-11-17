"""GC do caderno de trabalho (docs/working-memory-plan.md §3.3).

Task asyncio no molde do GC do File Store: a cada tick, nota ``active`` cujo
``expires_at`` já passou vira ``done`` — sai do índice (deixa de custar tokens
em todo turno) e continua legível por ``wm_read``. Nada é apagado; ``pinned``
nunca expira.

Nunca derruba o processo: exceção do tick é logada e o loop segue.
"""

from __future__ import annotations

import asyncio
import logging

from core.working_memory.store import WorkingMemoryStore

logger = logging.getLogger(__name__)


async def gc_loop(store: WorkingMemoryStore, interval_s: int = 21600) -> None:
    """Loop infinito do GC. Cancelado no shutdown do app."""
    logger.info("[WorkingMemory] GC iniciado (tick=%ds)", interval_s)
    while True:
        try:
            await asyncio.sleep(interval_s)
            result = await store.gc_pass()
            if result.get("archived"):
                logger.info("[WorkingMemory] GC: %d notas arquivadas", result["archived"])
        except asyncio.CancelledError:
            logger.info("[WorkingMemory] GC encerrado")
            raise
        except Exception as exc:  # noqa: BLE001 — um tick ruim não mata o loop
            logger.warning("[WorkingMemory] GC falhou neste tick: %s", exc)
