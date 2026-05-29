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
