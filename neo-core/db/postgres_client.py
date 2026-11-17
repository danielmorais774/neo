# app/db/postgres_client.py
import os
from typing import Optional
from uuid import UUID

from sqlmodel import Session, create_engine, select
from .models import User, UserMessage


class PostgresClient:
    """PostgreSQL client using SQLModel ORM."""
    
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.environ.get("DATABASE_URL", "postgresql://neo:neo@localhost:5432/neodb")
        self.engine = create_engine(self.dsn, echo=False)

    async def connect(self):
        """Initialize connection (SQLModel doesn't need async connect)."""
        pass

    async def disconnect(self):
        """Dispose engine."""
        self.engine.dispose()

    def get_session(self) -> Session:
        """Return a new SQLModel session."""
        return Session(self.engine)

    # ---------- User methods ----------
    def create_user(self, username: str, email: str | None = None, display_name: str | None = None) -> UUID:
        """Create a user or return existing user ID by username."""
        with self.get_session() as session:
            # Check existing
            statement = select(User).where(User.username == username)
            existing = session.exec(statement).first()
            if existing:
                return existing.id
            
            # Create new
            user = User(username=username, email=email, display_name=display_name)
            session.add(user)
            session.commit()
            session.refresh(user)
            return user.id

    def get_user_by_username(self, username: str) -> Optional[User]:
        """Get user by username."""
        with self.get_session() as session:
            statement = select(User).where(User.username == username)
            return session.exec(statement).first()

    def get_user_by_id(self, user_id: UUID) -> Optional[User]:
        """Get user by ID."""
        with self.get_session() as session:
            return session.get(User, user_id)

    # ---------- UserMessage methods ----------
    def save_user_message(
        self, 
        service: str, 
        content: str, 
        role: str = 'user',
        user_id: UUID | None = None, 
        metadata: dict | None = None
    ) -> UUID:
        """Save a message (user or assistant) and record source service (e.g. 'neo-desktop').

        Args:
            service: Origin service name (e.g., 'neo-desktop')
            content: Message text
            role: 'user' | 'assistant' | 'system' (default: 'user')
            user_id: Optional user UUID
            metadata: Optional metadata dict
        
        Returns:
            Message UUID
        """
        with self.get_session() as session:
            msg = UserMessage(
                user_id=user_id,
                service=service,
                role=role,
                content=content,
                extra_metadata=metadata
            )
            session.add(msg)
            session.commit()
            session.refresh(msg)
            return msg.id

    def fetch_user_messages(
        self, 
        user_id: UUID | None = None, 
        service: str | None = None, 
        role: str | None = None,
        limit: int = 100
    ) -> list[UserMessage]:
        """Fetch user messages with optional filters."""
        with self.get_session() as session:
            statement = select(UserMessage)
            
            if user_id:
                statement = statement.where(UserMessage.user_id == user_id)
            if service:
                statement = statement.where(UserMessage.service == service)
            if role:
                statement = statement.where(UserMessage.role == role)
            
            statement = statement.order_by(UserMessage.created_at.desc()).limit(limit)
            return list(session.exec(statement).all())

    def fetch_session_history(self, session_id: str, limit: int = 20) -> list[dict]:
        """Fetch recent messages for a session as [{role, content}] for LLM context."""
        with self.get_session() as session:
            statement = (
                select(UserMessage)
                .where(UserMessage.extra_metadata["session_id"].as_string() == session_id)
                .order_by(UserMessage.created_at.asc())
                .limit(limit)
            )
            messages = list(session.exec(statement).all())
            return [{"role": m.role, "content": m.content} for m in messages]

    # ---------- Legacy memory methods (kept for compatibility) ----------
    def save_memory(self, role: str, content: str):
        """Legacy: save to memories table (kept for backward compatibility)."""
        # Could be migrated to use a Memory model if needed
        pass

    def fetch_recent_memories(self, limit: int = 10):
        """Legacy: fetch from memories table."""
        pass
