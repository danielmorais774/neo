"""Audit logging for PKS mutations."""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class AuditLogger:
    async def log(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID | None,
        actor_type: str,
        action: str,
        target_type: str,
        target_id: UUID | None = None,
        actor_id: str | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        reason: str | None = None,
        confidence: float | None = None,
        source_message_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        try:
            await session.execute(
                text(
                    """
                    INSERT INTO pks_audit_log
                      (id, workspace_id, actor_type, actor_id, action, target_type, target_id,
                       before, after, reason, confidence, source_message_id, request_id, created_at)
                    VALUES
                      (:id, :workspace_id, :actor_type, :actor_id, :action, :target_type, :target_id,
                       CAST(:before AS jsonb), CAST(:after AS jsonb), :reason, :confidence,
                       :source_message_id, :request_id, NOW())
                    """
                ),
                {
                    "id": uuid4(),
                    "workspace_id": workspace_id,
                    "actor_type": actor_type,
                    "actor_id": actor_id,
                    "action": action,
                    "target_type": target_type,
                    "target_id": target_id,
                    "before": json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
                    "after": json.dumps(after, ensure_ascii=False, default=str) if after is not None else None,
                    "reason": reason,
                    "confidence": confidence,
                    "source_message_id": source_message_id,
                    "request_id": request_id,
                },
            )
        except Exception:
            logger.exception("[PKS] Failed to write audit log for %s/%s", target_type, action)
