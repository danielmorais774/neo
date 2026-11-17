"""Persistência dos atos de birra + reversão (§3.3 e §6.2 do plano).

O ledger existe por um motivo só: **nenhum ato de birra pode sobreviver ao
episódio**. Ele fica no Postgres, e não em memória, porque o neo-core roda com
``--reload`` e reinicia a cada save — um ``asyncio.create_task`` de reversão
morreria junto e a luz ficaria apagada.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlmodel import select

from db.models.birra_act import BirraAct

logger = logging.getLogger(__name__)


class BirraStore:
    """CRUD do ledger. Nunca levanta para o chamador do turno."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def record(
        self,
        *,
        user_id: UUID,
        session_id: UUID | None,
        episode_id: str,
        mecanica: str,
        fala: str,
        entity_id: str | None,
        requested: dict[str, Any],
        performed: dict[str, Any] | None = None,
        undo: dict[str, Any] | None = None,
        undo_expected_state: str | None = None,
        revert_after_s: int | None = None,
    ) -> BirraAct | None:
        act = BirraAct(
            user_id=user_id,
            session_id=session_id,
            episode_id=episode_id,
            mecanica=mecanica,
            fala=fala,
            entity_id=entity_id,
            requested=requested,
            performed=performed,
            undo=undo,
            undo_expected_state=undo_expected_state,
            revert_at=(
                datetime.utcnow() + timedelta(seconds=revert_after_s)
                if undo and revert_after_s
                else None
            ),
        )
        try:
            async with self._db.get_session() as session:
                session.add(act)
                await session.commit()
                await session.refresh(act)
            return act
        except Exception as exc:  # noqa: BLE001
            logger.warning("[BIRRA] falha ao gravar ato: %s", exc)
            return None

    async def pending(
        self,
        *,
        user_id: UUID | None = None,
        episode_id: str | None = None,
        due_only: bool = False,
    ) -> list[BirraAct]:
        """Atos com undo ainda não aplicado."""
        try:
            async with self._db.get_session() as session:
                stmt = select(BirraAct).where(
                    BirraAct.reverted_at.is_(None),  # type: ignore[union-attr]
                    BirraAct.undo.is_not(None),  # type: ignore[union-attr]
                )
                if user_id is not None:
                    stmt = stmt.where(BirraAct.user_id == user_id)
                if episode_id is not None:
                    stmt = stmt.where(BirraAct.episode_id == episode_id)
                if due_only:
                    stmt = stmt.where(
                        BirraAct.revert_at.is_not(None),  # type: ignore[union-attr]
                        BirraAct.revert_at <= datetime.utcnow(),  # type: ignore[operator]
                    )
                result = await session.execute(stmt)
                return list(result.scalars().all())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[BIRRA] falha ao listar pendentes: %s", exc)
            return []

    async def original_undo(self, episode_id: str, entity_id: str) -> BirraAct | None:
        """O PRIMEIRO ato do episódio nesta entidade — dono do estado original.

        Pedido repetido continua sofrendo birra, então a mesma entidade pode ser
        alterada duas vezes no episódio. O undo válido é sempre o do primeiro ato,
        senão "reverter" devolveria o ar a 26 °C em vez dos 24 °C originais.
        """
        try:
            async with self._db.get_session() as session:
                result = await session.execute(
                    select(BirraAct)
                    .where(
                        BirraAct.episode_id == episode_id,
                        BirraAct.entity_id == entity_id,
                        BirraAct.undo.is_not(None),  # type: ignore[union-attr]
                        BirraAct.reverted_at.is_(None),  # type: ignore[union-attr]
                    )
                    .order_by(BirraAct.created_at)  # type: ignore[arg-type]
                    .limit(1)
                )
                return result.scalars().first()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[BIRRA] falha ao buscar undo original: %s", exc)
            return None

    async def mark_reverted(
        self, act_id: UUID, *, reason: str, error: str | None = None
    ) -> None:
        try:
            async with self._db.get_session() as session:
                act = await session.get(BirraAct, act_id)
                if act is None:
                    return
                act.reverted_at = datetime.utcnow()
                act.revert_reason = reason[:16]
                act.revert_error = error
                session.add(act)
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[BIRRA] falha ao marcar revertido: %s", exc)
