"""GC do File Store (docs/file-store-plan.md §7).

Task asyncio no core (molde do ``_offline_checker``/JobManager): a cada tick,
``temp`` vencido vira ``expired`` e o blob some se ninguém mais o referencia.
A LINHA fica — o painel mostra o histórico; purge é manual.

Nunca derruba o processo: exceção do tick é logada e o loop segue.
"""

from __future__ import annotations

import asyncio
import logging

from core.files.store import FileStore

logger = logging.getLogger(__name__)


async def gc_loop(store: FileStore, interval_s: int = 3600) -> None:
    """Loop infinito do GC. Cancelado no shutdown do app."""
    logger.info("[files] GC iniciado (tick=%ds)", interval_s)
    while True:
        try:
            await asyncio.sleep(interval_s)
            result = await store.gc_pass()
            if result.get("expired"):
                logger.info(
                    "[files] GC: %d expirados, %d blobs removidos",
                    result["expired"], result["blobs_removed"],
                )
        except asyncio.CancelledError:
            logger.info("[files] GC encerrado")
            raise
        except Exception as exc:  # noqa: BLE001 — um tick ruim não mata o loop
            logger.warning("[files] GC falhou neste tick: %s", exc)
