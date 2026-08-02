"""BirraAct — ledger de desfazer do Modo Birra (docs/modo-birra-plan.md §6.2).

Uma linha por ATO de birra: o que o usuário pediu, o que o Neo fez no lugar e —
o ponto todo da tabela — **como desfazer**. Ato que alterou o mundo grava
``undo`` (a chamada que restaura o estado anterior) e ``revert_at``; um sweeper
reverte no prazo, ou antes disso quando o usuário pede desculpa.

Por que Postgres e não memória: o neo-core roda com ``--reload`` e reinicia a
cada save, o que mataria qualquer ``asyncio.create_task`` de reversão pendente.
Aqui a varredura de startup recupera.

``undo_expected_state`` é o compare-and-swap: se o usuário já mexeu no
dispositivo na mão, o undo é descartado em vez de brigar com o interruptor.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Column, String, Text, Uuid as SAUuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

# O que foi feito com o dispositivo. ``nada`` = birra só verbal (sem undo).
MECANICAS = ("nada", "inverte", "meia_bomba", "troca_alvo")
# Como a recusa/alteração é dita. A fala é do LLM; isto é só o rótulo da intenção.
FALAS = ("recusa_seca", "delega", "condiciona", "deboche", "pedagio")


class BirraAct(SQLModel, table=True):
    __tablename__ = "birra_act"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        sa_column=Column("user_id", SAUuid, nullable=False, index=True)
    )
    session_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("session_id", SAUuid, nullable=True)
    )
    # Episódio de birra (o mesmo id que vive no bloco ``birra`` do neo_affect).
    episode_id: str = Field(
        sa_column=Column("episode_id", String(36), nullable=False, index=True)
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow, sa_column=Column("created_at", nullable=False)
    )
    mecanica: str = Field(sa_column=Column("mecanica", String(12), nullable=False))
    fala: str = Field(sa_column=Column("fala", String(16), nullable=False))
    entity_id: Optional[str] = Field(
        default=None, sa_column=Column("entity_id", String(128), nullable=True)
    )
    # {domain, service, data} — o que o usuário pediu.
    requested: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("requested", JSONB, nullable=False, server_default="{}"),
    )
    # {domain, service, data} — o que foi de fato enviado ao HA. NULL em ``nada``.
    performed: Optional[dict[str, Any]] = Field(
        default=None, sa_column=Column("performed", JSONB, nullable=True)
    )
    # {domain, service, data} — a chamada que restaura o estado ANTERIOR.
    undo: Optional[dict[str, Any]] = Field(
        default=None, sa_column=Column("undo", JSONB, nullable=True)
    )
    # Estado que a birra deixou; o revert só age se a entidade ainda estiver assim.
    undo_expected_state: Optional[str] = Field(
        default=None, sa_column=Column("undo_expected_state", String(64), nullable=True)
    )
    # NULL quando não há o que desfazer (mecanica="nada"). O sweeper varre por aqui.
    revert_at: Optional[datetime] = Field(
        default=None, sa_column=Column("revert_at", nullable=True, index=True)
    )
    reverted_at: Optional[datetime] = Field(
        default=None, sa_column=Column("reverted_at", nullable=True)
    )
    # Motivo do fim: prazo | apology | escape | ttl | panic | reset | cas_skip
    revert_reason: Optional[str] = Field(
        default=None, sa_column=Column("revert_reason", String(16), nullable=True)
    )
    revert_error: Optional[str] = Field(
        default=None, sa_column=Column("revert_error", Text, nullable=True)
    )
