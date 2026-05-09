"""Connected device model — tracks devices registered via gRPC."""

import uuid
from datetime import datetime

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, String, Uuid as SAUuid


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
    os_platform: str = Field(sa_column=Column("os_platform", String(64), nullable=False))
    os_version: str = Field(sa_column=Column("os_version", String(128), nullable=False))
    client_version: str = Field(sa_column=Column("client_version", String(32), nullable=False, default=""))
    ip_address: str = Field(sa_column=Column("ip_address", String(64), nullable=False, default=""))
    last_heartbeat_unix: int = Field(default=0)
    is_online: bool = Field(default=False)
    ws_connection_id: str | None = Field(
        sa_column=Column("ws_connection_id", String(256), nullable=True, default=None),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", nullable=False),
    )
