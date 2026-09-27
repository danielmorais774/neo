"""Connected device model — tracks devices registered via gRPC.

As colunas de pareamento (``display_name``, ``pairing_token_hash``, ``paired_at``,
``capabilities``) são o que torna o ``user_id`` um DONO de verdade: até o
pareamento existir, o ``user_id`` era apenas declarado pelo cliente (o desktop
manda o que quiser em ``NEO_USER_ID``). Depois dele, o core resolve
``token → (user_id, device_id)`` e ignora o que o cliente afirma.
Ver docs/device-control-plan.md §1–§2.
"""

import uuid
from datetime import datetime

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, String, Uuid as SAUuid
from sqlalchemy.dialects.postgresql import JSONB


class ConnectedDevice(SQLModel, table=True):
    __tablename__ = "connected_device"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        sa_column=Column("user_id", SAUuid, nullable=False, index=True),
    )
    device_id: str = Field(
        sa_column=Column("device_id", String(256), nullable=False, unique=True),
    )
    device_type: str = Field(
        sa_column=Column("device_type", String(64), nullable=False),
    )
    model: str = Field(sa_column=Column("model", String(128), nullable=False))
    hardware_id: str = Field(
        sa_column=Column("hardware_id", String(256), nullable=False, default=""),
    )
    os_platform: str = Field(sa_column=Column("os_platform", String(64), nullable=False))
    os_version: str = Field(sa_column=Column("os_version", String(128), nullable=False))
    client_version: str = Field(sa_column=Column("client_version", String(32), nullable=False, default=""))
    ip_address: str = Field(sa_column=Column("ip_address", String(64), nullable=False, default=""))
    last_heartbeat_unix: int = Field(default=0)
    is_online: bool = Field(default=False)
    ws_connection_id: str | None = Field(
        sa_column=Column("ws_connection_id", String(256), nullable=True, default=None),
    )
    # ── Pareamento / controle (docs/device-control-plan.md) ──────────────
    # Nome dado pelo USUÁRIO ("PC da sala"). É por ele que o cérebro endereça o
    # device — nunca pelo device_id, que é um UUID opaco que o modelo inventa
    # (mesmo modo de falha do file_id e do entity_id do Home Assistant).
    display_name: str | None = Field(
        default=None,
        sa_column=Column("display_name", String(64), nullable=True, default=None),
    )
    # sha256 do token de pareamento. O token cru é mostrado UMA vez, no pair.
    pairing_token_hash: str | None = Field(
        default=None,
        sa_column=Column("pairing_token_hash", String(128), nullable=True, default=None),
    )
    paired_at: datetime | None = Field(
        default=None,
        sa_column=Column("paired_at", nullable=True, default=None),
    )
    # O que ESTE device sabe fazer, informado por ele no pareamento
    # ({"volume": true, "power": false, ...}). Descoberto, não chutado: sem isso
    # o cérebro oferece `shutdown` num device que não implementa e a tool falha
    # DEPOIS de prometer.
    capabilities: dict | None = Field(
        default=None,
        sa_column=Column("capabilities", JSONB, nullable=True, default=None),
    )
    # ── Superfície de navegador (docs/browser-surface-plan.md §3) ────────
    # `device_id` da MÁQUINA onde esta linha roda. Preenchido só em superfície de
    # navegador (a extensão), e preenchido pelo PAREAMENTO — nunca pelo que o
    # cliente declara: se a extensão pudesse dizer "sou do macbook", o vínculo
    # não valeria nada. Quem tem como provar a máquina é o desktop, que emite o
    # código com o próprio device_id estampado.
    #
    # É este campo que responde "qual extensão está na máquina que o usuário
    # chamou de macbook" quando o pedido vem do celular, sem desktop no canal.
    host_device_id: str | None = Field(
        default=None,
        sa_column=Column("host_device_id", String(256), nullable=True, default=None, index=True),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", nullable=False),
    )
