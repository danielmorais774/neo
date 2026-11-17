"""Workspace bootstrap for PKS."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from core.pks.repository import PKSRepository
from core.pks.schemas import UpsertEntityInput
from db.async_client import AsyncPostgresClient


DEFAULT_BOOTSTRAP_SPACES: tuple[tuple[str, str], ...] = (
    ("Perfil", "profile"),
    ("Pessoas", "people"),
    ("Projetos", "projects"),
    ("Dispositivos", "devices"),
    ("Academia", "fitness"),
    ("Documentos", "documents"),
    ("Preferências", "preferences"),
)


class PKSBootstrap:
    def __init__(self, db: AsyncPostgresClient, repository: PKSRepository) -> None:
        self.db = db
        self.repository = repository

    async def ensure_workspace(self, user_id: UUID) -> UUID:
        await self.db.ensure_user_exists(user_id)
        async with self.db.get_session() as session:
            user_row = await session.execute(
                text(
                    """
                    SELECT display_name, username, email
                    FROM "user"
                    WHERE id = :user_id
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            )
            user = user_row.mappings().first() or {}
            primary_name = (
                user.get("display_name")
                or user.get("username")
                or self._name_from_email(user.get("email"))
                or "Usuário principal"
            )

            workspace_id = await self.repository.ensure_workspace(
                session,
                user_id=user_id,
                name=primary_name,
            )

            profile_space_id: UUID | None = None
            for name, type_ in DEFAULT_BOOTSTRAP_SPACES:
                sid = await self.repository.ensure_space(session, workspace_id, name, type_)
                if type_ == "profile":
                    profile_space_id = sid

            await self.repository.upsert_entity(
                session,
                UpsertEntityInput(
                    workspace_id=workspace_id,
                    space_id=profile_space_id,
                    name=primary_name,
                    type="person",
                    aliases=["eu", "me", "usuário principal"],
                    metadata={"is_primary_user": True},
                    confidence=1.0,
                    sensitivity="private",
                    source="bootstrap",
                ),
            )
            await session.commit()
            return workspace_id

    @staticmethod
    def _name_from_email(email: str | None) -> str | None:
        if not email or "@" not in email:
            return None
        local = email.split("@", 1)[0].strip()
        return local or None
