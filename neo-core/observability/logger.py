"""ObservabilityLogger — async fire-and-forget logging to MongoDB.

Design principles:
  - NEVER raises or blocks the main flow — all writes are fire-and-forget
  - Structured documents per message_trace with full context
  - Lazy MongoDB connection (only if configured)
  - Graceful degradation when MongoDB is unavailable
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

# Singleton instance (set by api/main.py lifespan)
_instance: "ObservabilityLogger | None" = None


def get_obs() -> "ObservabilityLogger":
    """Return the global ObservabilityLogger instance."""
    global _instance
    if _instance is None:
        _instance = ObservabilityLogger()
    return _instance


def set_obs(instance: "ObservabilityLogger") -> None:
    """Set the global ObservabilityLogger instance."""
    global _instance
    _instance = instance


class MessageTrace:
    """Mutable trace for a single user message — built up during processing."""

    def __init__(self, session_id: str, user_message: str) -> None:
        self.trace_id = str(uuid4())
        self.session_id = session_id
        self.user_message = user_message
        self.started_at = datetime.now(timezone.utc)
        self.intent_routing: dict[str, Any] = {}
        self.topic_rotation: dict[str, Any] | None = None
        self.context_recovery: list[dict[str, Any]] = []
        self.llm_calls: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.pipeline_steps: list[dict[str, Any]] = []
        self.final_response: str | None = None
        self.error: str | None = None
        self._finished = False

    def set_intent(
        self,
        intent: str,
        confidence: float,
        explanation: str,
        tool_name: str | None = None,
        raw_payload: dict | None = None,
        duration_ms: float = 0,
        needs_memory: bool = True,
    ) -> None:
        self.intent_routing = {
            "intent": intent,
            "confidence": confidence,
            "explanation": explanation,
            "tool_name": tool_name,
            "classifier_payload": raw_payload,
            "duration_ms": duration_ms,
            "needs_memory": needs_memory,
        }

    def set_topic_rotation(
        self,
        rotation_result: dict[str, Any],
    ) -> None:
        """Record the topic rotation check data for debugging."""
        self.topic_rotation = rotation_result

    def set_context_recovery(
        self,
        results: list[Any],
    ) -> None:
        """Record recovered cross-session memories for debugging."""
        self.context_recovery = []
        for ctx in (results or []):
            entry: dict[str, Any] = {
                "content": getattr(ctx, "content", str(ctx))[:300],
                "score": round(getattr(ctx, "final_score", 0), 4),
                "source": getattr(ctx, "source", "?"),
            }
            sid = getattr(ctx, "session_id", None)
            if sid:
                entry["session_id"] = str(sid)[:8]
            created = getattr(ctx, "created_at", None)
            if created:
                entry["created_at"] = created.strftime("%Y-%m-%d %H:%M") if hasattr(created, "strftime") else str(created)[:16]
            self.context_recovery.append(entry)

    def add_llm_call(
        self,
        provider: str,
        model: str,
        messages_sent: list[dict],
        response_text: str | None,
        duration_ms: float,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        error: str | None = None,
        temperature: float | None = None,
        stage: str | None = None,
    ) -> None:
        self.llm_calls.append({
            "provider": provider,
            "model": model,
            "stage": stage,
            "temperature": temperature,
            "messages_sent": messages_sent,
            "response_text": response_text,
            "duration_ms": duration_ms,
            "tokens": {
                "prompt": prompt_tokens,
                "completion": completion_tokens,
                "total": prompt_tokens + completion_tokens,
            },
            "error": error,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def add_pipeline_step(
        self,
        index: int,
        intent: str,
        origin: str,
        input_text: str,
        final_text: str | None = None,
        handoffs: list[str] | None = None,
    ) -> None:
        """Record one orchestrator step (chain element) for the debug panel."""
        self.pipeline_steps.append({
            "index": index,
            "intent": intent,
            "origin": origin,
            "input": (input_text or "")[:500],
            "final_text": (final_text or "")[:500] if final_text else None,
            "handoffs": handoffs or [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def add_tool_call(
        self,
        tool_name: str,
        arguments: dict,
        result: Any,
        duration_ms: float,
        error: str | None = None,
    ) -> None:
        self.tool_calls.append({
            "tool_name": tool_name,
            "arguments": arguments,
            "result": result,
            "duration_ms": duration_ms,
            "error": error,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def finish(self, response: str | None = None, error: str | None = None) -> None:
        self.final_response = response
        self.error = error
        self._finished = True

    def to_document(self) -> dict[str, Any]:
        elapsed = (datetime.now(timezone.utc) - self.started_at).total_seconds() * 1000
        doc: dict[str, Any] = {
            "_id": self.trace_id,
            "session_id": self.session_id,
            "user_message": self.user_message,
            "started_at": self.started_at.isoformat(),
            "duration_ms": round(elapsed, 2),
            "intent_routing": self.intent_routing,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "final_response": self.final_response,
            "error": self.error,
        }
        if self.topic_rotation is not None:
            doc["topic_rotation"] = self.topic_rotation
        if self.context_recovery:
            doc["context_recovery"] = self.context_recovery
        if self.pipeline_steps:
            doc["pipeline_steps"] = self.pipeline_steps
        return doc


class ObservabilityLogger:
    """Async MongoDB logger — all operations are non-blocking fire-and-forget."""

    def __init__(self, mongo_url: str | None = None, db_name: str | None = None) -> None:
        from shared.config import get_settings
        settings = get_settings()
        self._mongo_url = mongo_url or settings.mongo_url
        self._db_name = db_name or settings.mongo_db
        self._client = None
        self._db = None
        self._available = False
        self._active_traces: dict[str, MessageTrace] = {}

    async def connect(self) -> None:
        """Initialize MongoDB connection. Called once at startup."""
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
            self._client = AsyncIOMotorClient(
                self._mongo_url,
                serverSelectionTimeoutMS=3000,
            )
            # Ping to verify connection
            await self._client.admin.command("ping")
            self._db = self._client[self._db_name]

            # Create indexes
            await self._db.message_traces.create_index("session_id")
            await self._db.message_traces.create_index("started_at")
            await self._db.debug_sessions.create_index("session_id", unique=True)
            await self._db.debug_sessions.create_index("last_activity_at")

            self._available = True
            logger.info(f"✅ ObservabilityLogger connected to MongoDB: {self._db_name}")
        except Exception as e:
            logger.warning(f"⚠️  ObservabilityLogger: MongoDB unavailable ({e}) — debug logging disabled")
            self._available = False

    async def disconnect(self) -> None:
        if self._client:
            self._client.close()

    # ---------------------------------------------------------------
    # Session tracking
    # ---------------------------------------------------------------

    def _fire(self, coro) -> None:
        """Schedule a coroutine as a background task — never awaited by caller."""
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(self._safe_run(coro))
        except RuntimeError:
            pass  # No event loop — skip silently

    async def _safe_run(self, coro) -> None:
        try:
            await coro
        except Exception as e:
            logger.debug(f"ObservabilityLogger background write failed: {e}")

    async def _upsert_session(self, session_id: str, service: str, status: str) -> None:
        if not self._available:
            return
        try:
            now = datetime.now(timezone.utc).isoformat()
            await self._db.debug_sessions.update_one(
                {"session_id": session_id},
                {
                    "$set": {"last_activity_at": now, "status": status, "service": service},
                    "$setOnInsert": {"session_id": session_id, "created_at": now},
                    "$inc": {"message_count": 1},
                },
                upsert=True,
            )
        except Exception as e:
            logger.debug(f"Obs upsert_session failed: {e}")

    # ---------------------------------------------------------------
    # Public API — all fire-and-forget
    # ---------------------------------------------------------------

    def start_trace(self, session_id: str, user_message: str, service: str = "neo-desktop") -> MessageTrace:
        """Create and register a trace for a user message."""
        trace = MessageTrace(session_id=session_id, user_message=user_message)
        self._active_traces[trace.trace_id] = trace
        self._fire(self._upsert_session(session_id, service, "active"))
        return trace

    def finish_trace(self, trace: MessageTrace, response: str | None = None, error: str | None = None) -> None:
        """Finalize trace and save to MongoDB."""
        trace.finish(response=response, error=error)
        self._active_traces.pop(trace.trace_id, None)
        self._fire(self._save_trace(trace))

    async def _save_trace(self, trace: MessageTrace) -> None:
        if not self._available:
            return
        try:
            doc = trace.to_document()
            await self._db.message_traces.insert_one(doc)
        except Exception as e:
            logger.debug(f"Obs save_trace failed: {e}")
