"""Reversão dos atos de birra — a trava que torna a feature segura (§3.3).

Duas entradas: o sweeper periódico (prazo vencido) e a reversão imediata
(desculpa, ordem enfática, pânico). As duas passam pelo mesmo
``revert_act``, que faz **compare-and-swap**: se o usuário já mexeu no
dispositivo na mão, o undo é descartado — o Neo não fica brigando com o
interruptor.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from core.agent.birra.store import BirraStore
from db.models.birra_act import BirraAct

logger = logging.getLogger(__name__)


async def revert_act(act: BirraAct, adapter: Any, store: BirraStore, *, reason: str) -> bool:
    """Desfaz um ato. Devolve True quando a chamada de restauração foi enviada."""
    undo = act.undo or {}
    domain, service = undo.get("domain"), undo.get("service")
    if not domain or not service or not act.entity_id:
        await store.mark_reverted(act.id, reason="cas_skip", error="undo incompleto")
        return False

    # Compare-and-swap: só restaura se o mundo ainda estiver como a birra deixou.
    if act.undo_expected_state:
        try:
            entity = await adapter.get_state(act.entity_id)
            current = str(entity.get("state") or "")
            if current != act.undo_expected_state:
                logger.info(
                    "[BIRRA] revert skip (cas) entity=%s esperado=%s atual=%s",
                    act.entity_id, act.undo_expected_state, current,
                )
                await store.mark_reverted(act.id, reason="cas_skip")
                return False
        except Exception as exc:  # noqa: BLE001 — leitura falhou: restaura mesmo assim
            logger.warning("[BIRRA] revert: leitura de %s falhou (%s)", act.entity_id, exc)

    payload = dict(undo.get("data") or {})
    payload["entity_id"] = act.entity_id
    try:
        await adapter.call_service(domain, service, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BIRRA] revert FALHOU entity=%s: %s", act.entity_id, exc)
        await store.mark_reverted(act.id, reason=reason, error=str(exc)[:500])
        return False
    logger.info("[BIRRA] revert ok entity=%s reason=%s", act.entity_id, reason)
    await store.mark_reverted(act.id, reason=reason)
    return True


async def revert_pending(
    store: BirraStore,
    adapter: Any,
    *,
    user_id: UUID | None = None,
    episode_id: str | None = None,
    due_only: bool = False,
    reason: str = "prazo",
) -> int:
    """Reverte tudo que estiver pendente no escopo pedido. Devolve quantos foram."""
    if adapter is None or not getattr(adapter, "is_configured", False):
        return 0
    acts = await store.pending(user_id=user_id, episode_id=episode_id, due_only=due_only)
    done = 0
    for act in acts:
        if await revert_act(act, adapter, store, reason=reason):
            done += 1
    return done


async def sweeper_loop(
    store: BirraStore,
    adapter_factory: Any,
    interval_s: int = 30,
) -> None:
    """Loop do sweeper. Um tick ruim nunca mata o loop (molde do GC de arquivos).

    Roda também UMA vez no começo: atos pendentes de antes de um restart (que é
    o caso comum aqui — o core recarrega a cada save) são recuperados.
    """
    logger.info("[BIRRA] sweeper iniciado (tick=%ds)", interval_s)
    first = True
    while True:
        try:
            if not first:
                await asyncio.sleep(interval_s)
            first = False
            adapter = adapter_factory()
            done = await revert_pending(store, adapter, due_only=True, reason="prazo")
            if done:
                logger.info("[BIRRA] sweeper reverteu %d ato(s)", done)
        except asyncio.CancelledError:
            logger.info("[BIRRA] sweeper encerrado")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[BIRRA] sweeper falhou neste tick: %s", exc)
