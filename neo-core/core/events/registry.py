"""Registry das inscrições (docs/playlists-skill-plan.md §7.3).

SQL direto sobre ``event_subscription``, sem cache: a tabela é pequena, o acesso
é indexado por ``(device_id, channel, state)`` e o estado muda a cada faixa —
cache aqui só criaria o bug de despachar a partir de uma inscrição já encerrada.

Toda leitura já aplica o TTL: uma inscrição vencida nunca é devolvida como
armada, mesmo que ninguém tenha passado para apagá-la.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

_COLUMNS = (
    "id, user_id, device_id, channel, events, target, expected_ref, handler_ref, "
    "handler_snapshot, state, reason, fires, max_fires, consecutive_errors, current_code, "
    "session_id, expires_at, last_event_at, last_event_key, enabled, created_at, updated_at"
)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except ValueError:
            return []
    return []


class Subscription:
    """Uma inscrição carregada do banco."""

    __slots__ = (
        "id", "user_id", "device_id", "channel", "events", "target", "expected_ref",
        "handler_ref", "handler_snapshot", "state", "reason", "fires", "max_fires",
        "consecutive_errors", "current_code", "session_id", "expires_at", "last_event_at",
        "last_event_key", "enabled", "created_at", "updated_at",
    )

    def __init__(self, row: Any) -> None:
        self.id: uuid.UUID = row.id
        self.user_id: uuid.UUID | None = row.user_id
        self.device_id: str = row.device_id or ""
        self.channel: str = row.channel
        self.events: list[str] = [str(e) for e in _as_list(row.events)]
        self.target: dict[str, Any] = _as_dict(row.target)
        self.expected_ref: str = row.expected_ref or ""
        self.handler_ref: str = row.handler_ref
        self.handler_snapshot: dict[str, Any] = _as_dict(row.handler_snapshot)
        self.state: str = row.state
        self.reason: str = row.reason or ""
        self.fires: int = int(row.fires or 0)
        self.max_fires: int = int(row.max_fires or 0)
        self.consecutive_errors: int = int(row.consecutive_errors or 0)
        self.current_code: str = getattr(row, "current_code", "") or ""
        self.session_id: uuid.UUID | None = row.session_id
        self.expires_at: datetime | None = row.expires_at
        self.last_event_at: datetime | None = row.last_event_at
        self.last_event_key: str = row.last_event_key or ""
        self.enabled: bool = bool(row.enabled)
        self.created_at: datetime | None = row.created_at
        self.updated_at: datetime | None = row.updated_at

    @property
    def skill(self) -> str:
        return (self.handler_ref or "").split(".", 1)[0]

    @property
    def trigger_name(self) -> str:
        parts = (self.handler_ref or "").split(".", 1)
        return parts[1] if len(parts) == 2 else ""

    def is_live(self, *, now: datetime | None = None) -> bool:
        """Armada, habilitada e dentro do prazo."""
        now = now or datetime.utcnow()
        if self.state != "armada" or not self.enabled:
            return False
        return not (self.expires_at and self.expires_at <= now)

    def matches(self, *, event: str, target: dict[str, Any]) -> bool:
        """O evento é DESTA inscrição? (evento declarado + mesma aba/site)

        Alvo declarado que o evento não traz é tratado como "não bate": um
        evento sem `tab_id` não pode reivindicar uma inscrição presa a uma aba.
        """
        if event and self.events and event not in self.events:
            return False
        for key, expected in (self.target or {}).items():
            if expected in (None, ""):
                continue
            got = (target or {}).get(key)
            if got in (None, ""):
                return False
            if str(got) != str(expected):
                return False
        return True


class SubscriptionRegistry:
    """CRUD das inscrições. Sem regra de negócio — quem decide é o EventRouter."""

    def __init__(self, *, db: Any) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Leitura
    # ------------------------------------------------------------------

    async def get(self, subscription_id: uuid.UUID) -> Subscription | None:
        async with self._db.get_session() as session:
            row = (
                await session.execute(
                    text(f"SELECT {_COLUMNS} FROM event_subscription WHERE id = :id"),
                    {"id": subscription_id},
                )
            ).fetchone()
        return Subscription(row) if row is not None else None

    async def list(
        self,
        *,
        user_id: uuid.UUID | None = None,
        device_id: str | None = None,
        channel: str | None = None,
        states: tuple[str, ...] = ("armada", "pausada"),
        limit: int = 50,
    ) -> list[Subscription]:
        clauses = ["1 = 1"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
        if user_id is not None:
            clauses.append("user_id = :user_id")
            params["user_id"] = user_id
        if device_id:
            clauses.append("device_id = :device_id")
            params["device_id"] = device_id
        if channel:
            clauses.append("channel = :channel")
            params["channel"] = channel
        if states:
            clauses.append("state = ANY(:states)")
            params["states"] = list(states)
        async with self._db.get_session() as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {_COLUMNS} FROM event_subscription "
                        f"WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT :limit"
                    ),
                    params,
                )
            ).fetchall()
        return [Subscription(r) for r in rows]

    async def live_for_event(
        self, *, device_id: str, channel: str, event: str, target: dict[str, Any]
    ) -> Subscription | None:
        """A inscrição armada deste aparelho que casa com o evento (a mais nova)."""
        candidates = await self.list(
            device_id=device_id, channel=channel, states=("armada",), limit=20
        )
        now = datetime.utcnow()
        for sub in candidates:
            if sub.is_live(now=now) and sub.matches(event=event, target=target):
                return sub
        return None

    # ------------------------------------------------------------------
    # Escrita
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        user_id: uuid.UUID | None,
        device_id: str,
        channel: str,
        events: list[str],
        target: dict[str, Any],
        handler_ref: str,
        handler_snapshot: dict[str, Any] | None = None,
        expected_ref: str = "",
        current_code: str = "",
        session_id: uuid.UUID | None = None,
        ttl_minutes: int = 120,
        max_fires: int = 200,
    ) -> Subscription:
        sub_id = uuid.uuid4()
        now = datetime.utcnow()
        params = {
            "id": sub_id,
            "user_id": user_id,
            "device_id": device_id or "",
            "channel": channel,
            "events": json.dumps(list(events or [])),
            "target": json.dumps(target or {}),
            "expected_ref": expected_ref or "",
            "handler_ref": handler_ref,
            "handler_snapshot": json.dumps(handler_snapshot or {}),
            "state": "armada",
            "reason": "",
            "fires": 0,
            "max_fires": int(max_fires),
            "consecutive_errors": 0,
            "current_code": current_code or "",
            "session_id": session_id,
            "expires_at": now + timedelta(minutes=max(1, int(ttl_minutes))),
            "last_event_key": "",
            "enabled": True,
            "created_at": now,
            "updated_at": now,
        }
        columns = ", ".join(params)
        placeholders = ", ".join(f":{c}" for c in params)
        async with self._db.get_session() as session:
            await session.execute(
                text(
                    f"INSERT INTO event_subscription ({columns}) VALUES ({placeholders})"
                ),
                params,
            )
            await session.commit()
        created = await self.get(sub_id)
        if created is None:  # pragma: no cover — insert que não relê é bug de infra
            raise RuntimeError("inscrição criada mas não encontrada de volta")
        return created

    async def update(self, subscription_id: uuid.UUID, **fields: Any) -> Subscription | None:
        """Update parcial. ``events``/``target``/``handler_snapshot`` viram JSON."""
        if not fields:
            return await self.get(subscription_id)
        payload: dict[str, Any] = {}
        for key, value in fields.items():
            if key in ("events", "target", "handler_snapshot"):
                payload[key] = json.dumps(value if value is not None else ({} if key != "events" else []))
            else:
                payload[key] = value
        payload["updated_at"] = datetime.utcnow()
        sets = ", ".join(f"{c} = :{c}" for c in payload)
        async with self._db.get_session() as session:
            await session.execute(
                text(f"UPDATE event_subscription SET {sets} WHERE id = :__id"),
                {**payload, "__id": subscription_id},
            )
            await session.commit()
        return await self.get(subscription_id)

    async def end(self, subscription_id: uuid.UUID, reason: str) -> Subscription | None:
        return await self.update(subscription_id, state="encerrada", reason=reason[:500])

    async def pause(self, subscription_id: uuid.UUID, reason: str) -> Subscription | None:
        return await self.update(subscription_id, state="pausada", reason=reason[:500])

    async def end_many(
        self,
        *,
        user_id: uuid.UUID | None,
        channel: str | None = None,
        device_id: str | None = None,
        handler_ref: str | None = None,
        reason: str = "encerrada pelo usuário",
    ) -> int:
        """Kill switch: encerra tudo que casar. Devolve quantas foram."""
        clauses = ["state <> 'encerrada'"]
        params: dict[str, Any] = {"reason": reason[:500], "now": datetime.utcnow()}
        if user_id is not None:
            clauses.append("user_id = :user_id")
            params["user_id"] = user_id
        if channel:
            clauses.append("channel = :channel")
            params["channel"] = channel
        if device_id:
            clauses.append("device_id = :device_id")
            params["device_id"] = device_id
        if handler_ref:
            clauses.append("handler_ref = :handler_ref")
            params["handler_ref"] = handler_ref
        async with self._db.get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE event_subscription SET state = 'encerrada', reason = :reason, "
                    f"updated_at = :now WHERE {' AND '.join(clauses)}"
                ),
                params,
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    # ------------------------------------------------------------------
    # Trilha de entrega
    # ------------------------------------------------------------------

    async def log_delivery(
        self,
        *,
        subscription: Subscription | None,
        user_id: uuid.UUID | None,
        channel: str,
        event: str,
        event_key: str,
        outcome: str,
        reason: str = "",
        action: dict[str, Any] | None = None,
    ) -> None:
        """Nunca levanta: perder a trilha não pode derrubar o despacho."""
        params = {
            "id": uuid.uuid4(),
            "subscription_id": subscription.id if subscription else None,
            "user_id": user_id,
            "channel": channel[:32],
            "event": (event or "")[:40],
            "event_key": (event_key or "")[:160],
            "outcome": (outcome or "")[:20],
            "reason": (reason or "")[:1000],
            "action": json.dumps(action or {}),
            "created_at": datetime.utcnow(),
        }
        columns = ", ".join(params)
        placeholders = ", ".join(f":{c}" for c in params)
        try:
            async with self._db.get_session() as session:
                await session.execute(
                    text(f"INSERT INTO event_delivery ({columns}) VALUES ({placeholders})"),
                    params,
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[events] não gravei a entrega (%s): %s", outcome, exc)

    async def deliveries(
        self, *, user_id: uuid.UUID | None = None, subscription_id: uuid.UUID | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        clauses = ["1 = 1"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
        if user_id is not None:
            clauses.append("user_id = :user_id")
            params["user_id"] = user_id
        if subscription_id is not None:
            clauses.append("subscription_id = :subscription_id")
            params["subscription_id"] = subscription_id
        async with self._db.get_session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, subscription_id, channel, event, event_key, outcome, "
                        "reason, action, created_at FROM event_delivery "
                        f"WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT :limit"
                    ),
                    params,
                )
            ).mappings().fetchall()
        return [
            {
                "id": str(r["id"]),
                "inscricao": str(r["subscription_id"]) if r["subscription_id"] else None,
                "canal": r["channel"],
                "evento": r["event"],
                "resultado": r["outcome"],
                "motivo": r["reason"],
                "acao": _as_dict(r["action"]),
                "quando": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
