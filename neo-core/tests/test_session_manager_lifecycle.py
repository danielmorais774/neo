from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from core.session.session_manager import PersistentSessionManager
from db.models.conversation import ConversationSession


class _FakeEmbeddings:
    available = False


class _FakeDB:
    def __init__(self, existing: ConversationSession | None = None) -> None:
        self.sessions: dict = {}
        if existing:
            self.sessions[existing.id] = existing
        self.created: list[ConversationSession] = []
        self.closed_updates: list[ConversationSession] = []
        self.histories: dict = {}

    async def get_conversation_session(self, session_id):
        return self.sessions.get(session_id)

    async def create_conversation_session(
        self,
        session_id=None,
        user_id=None,
        service="neo-desktop",
        metadata=None,
    ):
        session = ConversationSession(
            id=session_id or uuid4(),
            user_id=user_id,
            service=service,
            extra_metadata=metadata,
        )
        self.sessions[session.id] = session
        self.created.append(session)
        return session

    async def fetch_session_messages(self, session_id, limit=50):
        return []

    async def fetch_session_history(self, session_id, limit=20):
        return self.histories.get(session_id, [])[-limit:]

    async def fetch_recent_sessions(
        self,
        user_id=None,
        service="neo-desktop",
        limit=5,
        statuses=None,
        exclude_session_id=None,
    ):
        sessions = [
            session for session in self.sessions.values()
            if session.service == service and session.id != exclude_session_id
        ]
        if user_id:
            sessions = [session for session in sessions if session.user_id == user_id]
        if statuses:
            sessions = [session for session in sessions if session.status in statuses]
        sessions.sort(key=lambda session: session.last_activity_at, reverse=True)
        return sessions[:limit]

    async def update_session(self, session):
        self.sessions[session.id] = session
        if session.status == "closed":
            self.closed_updates.append(session)


def _manager(db: _FakeDB) -> PersistentSessionManager:
    return PersistentSessionManager(
        db=db,
        embedding_service=_FakeEmbeddings(),
        hybrid_search=None,
        token_counter=None,
    )


@pytest.mark.asyncio
async def test_expired_explicit_session_creates_new_backend_session():
    old_id = uuid4()
    old = ConversationSession(
        id=old_id,
        status="active",
        message_count=4,
        last_activity_at=datetime.utcnow() - timedelta(hours=2),
    )
    db = _FakeDB(old)

    session = await _manager(db).get_or_create_session(
        session_id=old_id,
        user_id=None,
        service="neo-desktop",
    )

    assert session.id != old_id
    assert old.status == "closed"
    assert old.extra_metadata["close_reason"] == "inactivity"
    assert session.extra_metadata["created_after_expired_session_id"] == str(old_id)


@pytest.mark.asyncio
async def test_unknown_explicit_session_id_is_accepted_for_new_chat():
    requested_id = uuid4()
    db = _FakeDB()

    session = await _manager(db).get_or_create_session(
        session_id=requested_id,
        user_id=None,
        service="neo-desktop",
    )

    assert session.id == requested_id
    assert session.status == "active"


def test_detect_context_dependency_for_elliptical_followups():
    detector = PersistentSessionManager.detect_context_dependency

    assert detector("continua")["is_context_dependent"] is True
    assert detector("Eu estava pensando e não gostei muito dessa forma como me passou")["is_context_dependent"] is True
    assert detector("voltando ao assunto, queria ajustar o pipeline")["kind"] == "explicit_return"
    assert detector("qual é a capital da França?")["is_context_dependent"] is False


@pytest.mark.asyncio
async def test_expired_session_continuity_injects_previous_history():
    old_id = uuid4()
    new_session = ConversationSession(
        id=uuid4(),
        status="active",
        message_count=0,
        extra_metadata={"created_after_expired_session_id": str(old_id)},
    )
    old_session = ConversationSession(
        id=old_id,
        status="closed",
        message_count=2,
        last_activity_at=datetime.utcnow() - timedelta(hours=2),
    )
    db = _FakeDB(old_session)
    db.sessions[new_session.id] = new_session
    db.histories[old_id] = [
        {"role": "user", "content": "Vamos ajustar o pipeline de memória V2", "token_count": 10},
        {"role": "assistant", "content": "Certo, vou analisar a sessão.", "token_count": 8},
    ]

    result = await _manager(db).resolve_continuity_context(
        session=new_session,
        message="continua",
        user_id=None,
        service="neo-desktop",
    )

    assert result["action"] == "inject"
    assert result["source_session_id"] == str(old_id)
    assert result["history"][0]["content"] == "Vamos ajustar o pipeline de memória V2"


@pytest.mark.asyncio
async def test_new_chat_short_followup_does_not_silently_inherit_context():
    old_session = ConversationSession(
        id=uuid4(),
        status="closed",
        message_count=2,
        last_activity_at=datetime.utcnow() - timedelta(minutes=10),
        topic_summary="pipeline de memória V2",
    )
    new_session = ConversationSession(id=uuid4(), status="active", message_count=0)
    db = _FakeDB(old_session)
    db.sessions[new_session.id] = new_session

    result = await _manager(db).resolve_continuity_context(
        session=new_session,
        message="continua",
        user_id=None,
        service="neo-desktop",
    )

    assert result["action"] == "none"
