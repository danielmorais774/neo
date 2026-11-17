"""Single choke point for Neo's post-turn affect updates.

Every channel that finishes a user turn (desktop/voice via ``process_user_input``,
WhatsApp inbound via its pipeline) schedules the update HERE instead of rolling
its own background task. That buys, in one place:

  * **Zero hot-path impact** — ``schedule`` only creates a task; all LLM/DB work
    happens after the reply was already yielded.
  * **Tracked tasks** — strong references + done callbacks, so tasks can't be
    garbage-collected mid-flight (documented asyncio gotcha with bare
    ``create_task``) and can be drained/observed.
  * **Timeout** — the appraisal is capped (``neo_affect_appraisal_timeout_s``);
    on timeout the deterministic wrong-name/threat floors still persist, so the
    reaction is never lost, only its LLM nuance.
  * **Per-user serialization** — an ``asyncio.Lock`` per user makes the
    read-modify-write in ``update_user_affect`` safe against burst turns (voice/
    Live mode), which previously could lose one turn's deltas.
  * **Dedup** — a turn is appraised once even if two code paths schedule it
    (keyed by ``source_message_id``).
  * **Health** — failures are counted and surfaced via :func:`get_affect_health`
    so the panel can show silent degradation instead of hiding it in logs.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from core.agent.affect import (
    AXES,
    AffectAppraiser,
    AffectSignal,
    AffectTuning,
    apply_decay,
    detect_threat,
    detect_wrong_name,
)

if TYPE_CHECKING:
    from core.llm.router import LLMRouter
    from shared.config import Settings

logger = logging.getLogger(__name__)

#: Internal fallback replies that must not be fed to the appraiser as if Neo
#: actually said them (the turn errored; rate the user message alone).
#: "Não consegui responder agora" é o TURN_FAILED_REPLY do ai_service — o texto
#: que fecha um turno derrubado por falha de provider.
_ERROR_RESPONSE_PREFIXES = (
    "Erro ao processar",
    "Desculpe, não consegui gerar",
    "Não consegui responder agora",
)

_health: dict[str, Any] = {
    "appraisals": 0,
    "updates": 0,
    "failures": 0,
    "last_error": None,
    "last_error_at": None,
    "last_update_at": None,
}

_shared: "AffectUpdater | None" = None


def get_affect_health() -> dict[str, Any]:
    """Snapshot of appraisal health for the panel/debug endpoint."""
    shared = _shared
    return {
        **_health,
        "pending_tasks": len(shared._tasks) if shared else 0,
    }


def set_shared_updater(updater: "AffectUpdater | None") -> None:
    """Register the process-wide updater so out-of-band channels (WhatsApp
    pipeline) can reach it without threading it through every constructor."""
    global _shared
    _shared = updater


def get_shared_updater() -> "AffectUpdater | None":
    return _shared


class AffectUpdater:
    """Owns the post-turn affect update lifecycle. One instance per process."""

    def __init__(
        self,
        llm_router: "LLMRouter",
        settings: "Settings",
        pks: Any,
        db: Any | None = None,
    ) -> None:
        self._appraiser = AffectAppraiser(llm_router, settings, db=db)
        self._settings = settings
        self._pks = pks
        self._tasks: set[asyncio.Task] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        # Ring of recently scheduled turn ids (dedup across code paths).
        self._seen_ids: OrderedDict[str, None] = OrderedDict()

    # ── Scheduling ────────────────────────────────────────────────────────

    def schedule(
        self,
        *,
        user_message: str,
        assistant_response: str | None,
        user_id: UUID | None,
        source: str = "desktop",
        source_message_id: str | None = None,
        slog: Any | None = None,
    ) -> bool:
        """Fire-and-forget appraisal of one finished turn. Returns True if scheduled."""
        if self._settings.neo_affect_enabled is not True:
            return False
        if not user_id or not (user_message or "").strip():
            return False
        if source_message_id:
            if source_message_id in self._seen_ids:
                return False
            self._seen_ids[source_message_id] = None
            while len(self._seen_ids) > 128:
                self._seen_ids.popitem(last=False)
        task = asyncio.create_task(
            self._run(
                user_message=user_message,
                assistant_response=assistant_response or "",
                user_id=user_id,
                source=source,
                source_message_id=source_message_id,
                slog=slog,
            ),
            name=f"affect-update:{source}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def drain(self, timeout: float = 5.0) -> None:
        """Wait for in-flight updates (graceful shutdown); best-effort."""
        if not self._tasks:
            return
        await asyncio.wait(list(self._tasks), timeout=timeout)

    # ── Internals ─────────────────────────────────────────────────────────

    def _lock_for(self, user_id: UUID) -> asyncio.Lock:
        key = str(user_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
            # Personal assistant: user count is tiny; no eviction needed. Guard
            # anyway so a pathological caller can't grow this unbounded.
            if len(self._locks) > 256:
                self._locks.pop(next(iter(self._locks)))
        return lock

    async def _run(
        self,
        *,
        user_message: str,
        assistant_response: str,
        user_id: UUID,
        source: str,
        source_message_id: str | None,
        slog: Any | None,
    ) -> None:
        try:
            wrong_name = detect_wrong_name(user_message)
            threat = detect_threat(user_message)

            if assistant_response.startswith(_ERROR_RESPONSE_PREFIXES):
                assistant_response = ""

            tuning = AffectTuning.from_settings(self._settings)
            current = await self._pks.get_user_affect(user_id=user_id)
            # The appraiser should see today's numbers, not yesterday's raw ones.
            current = apply_decay(current, tuning)

            try:
                signal = await asyncio.wait_for(
                    self._appraiser.appraise(
                        user_message=user_message,
                        assistant_response=assistant_response,
                        current=current,
                    ),
                    timeout=self._settings.neo_affect_appraisal_timeout_s,
                )
                _health["appraisals"] += 1
            except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
                # Deterministic floors below still fire; only the nuance is lost.
                signal = AffectSignal()
                self._record_failure(f"appraisal {type(e).__name__}: {e}")

            if signal.is_neutral and not wrong_name and not threat:
                return

            async with self._lock_for(user_id):
                updated = await self._pks.update_user_affect(
                    user_id=user_id,
                    signal=signal,
                    wrong_name=wrong_name,
                    threat=threat,
                    source_message_id=source_message_id,
                )
            if updated is None:
                return
            _health["updates"] += 1
            _health["last_update_at"] = datetime.now(timezone.utc).isoformat()
            if slog:
                vector = " ".join(
                    f"{axis}={getattr(updated.stats, axis):.0f}" for axis in AXES
                )
                slog.log_event(
                    "Estado Emocional",
                    f"[{source}] trigger={updated.last_trigger} {vector}"
                    + (f" ← {signal.why}" if signal.why else ""),
                )
        except Exception as e:
            self._record_failure(f"update: {e}")
            logger.warning("[Affect] Post-turn update failed: %s", e, exc_info=True)

    @staticmethod
    def _record_failure(message: str) -> None:
        _health["failures"] += 1
        _health["last_error"] = message[:300]
        _health["last_error_at"] = datetime.now(timezone.utc).isoformat()
        logger.warning("[Affect] %s", message)
