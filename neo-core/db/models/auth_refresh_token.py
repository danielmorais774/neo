"""Refresh token do login de usuário (docs/api-auth-plan.md §5).

**Por que uma tabela, e não um JWT longo.** Um access token de 30 min que vaza
morre sozinho; um token de 30 dias, não. Revogar um JWT exige lista de revogados
— ou seja, a tabela de qualquer jeito. Então o refresh é **opaco** (32 bytes
aleatórios), o banco guarda só o ``sha256``, e "sair de todos os aparelhos" é um
UPDATE.

``rotated_to`` guarda a rotação: cada refresh usado emite outro e marca o antigo.
Um refresh já usado que reaparece é sinal de token roubado — e aí a linha inteira
da família pode ser revogada.

``db/models/`` não importa de ``core`` (o alembic importa os models direto e
``core/__init__`` puxa a engine inteira — circular import que quebra a migração).
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, String, Uuid as SAUuid
from sqlmodel import Field, SQLModel


class AuthRefreshToken(SQLModel, table=True):
    __tablename__ = "auth_refresh_token"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        sa_column=Column("user_id", SAUuid, nullable=False, index=True),
    )
    #: sha256 do token cru. O token cru sai UMA vez, na resposta do login.
    token_hash: str = Field(
        sa_column=Column("token_hash", String(64), nullable=False, unique=True, index=True),
    )
    #: De onde veio ("neo-panel", user-agent cortado). Só para você reconhecer a
    #: sessão numa lista — nunca é usado para autorizar nada.
    origem: str = Field(sa_column=Column("origem", String(128), nullable=False, default=""))
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", nullable=False),
    )
    expires_at: datetime = Field(sa_column=Column("expires_at", nullable=False))
    last_used_at: Optional[datetime] = Field(
        default=None, sa_column=Column("last_used_at", nullable=True)
    )
    revoked_at: Optional[datetime] = Field(
        default=None, sa_column=Column("revoked_at", nullable=True)
    )
    rotated_to: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column("rotated_to", SAUuid, nullable=True)
    )
