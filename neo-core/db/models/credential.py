"""Credential — cofre de segredos cifrados (docs/service-gateway-dsl-plan.md §3.2).

Store ÚNICO do Neo, compartilhado com o browser-agent (browser-agent-plan §3.5):
os dois sistemas referenciam a mesma linha por ``fernet://<id>``, em vez de cada
um ter seu cofre.

O valor é cifrado com Fernet (AES-128-CBC + HMAC) e a chave (``NEO_CRED_KEY``)
mora SÓ na env da VPS — nunca no banco. Um dump do Postgres, sozinho, não
entrega segredo nenhum. ``value_encrypted`` jamais sai daqui em claro: quem lê é
o CredentialStore, e só para o executor, no momento da chamada.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, LargeBinary, String
from sqlmodel import Field, SQLModel

# Para que serve o segredo — orienta a UI e o consumidor, não muda a cifra.
CREDENTIAL_KINDS = ("token", "password", "api_key", "basic", "cookie", "cert", "other")


class Credential(SQLModel, table=True):
    __tablename__ = "credential"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Rótulo humano e único ("github-personal", "proxmox-root") — é o que o
    # painel mostra e o que aparece em listagens. NUNCA o valor.
    label: str = Field(sa_column=Column("label", String(80), nullable=False, unique=True))
    kind: str = Field(default="token", sa_column=Column("kind", String(20), nullable=False))
    description: Optional[str] = Field(
        default=None, sa_column=Column("description", String(300), nullable=True)
    )
    # Fernet token (bytes). Sem a NEO_CRED_KEY isto é lixo criptográfico.
    value_encrypted: bytes = Field(
        sa_column=Column("value_encrypted", LargeBinary, nullable=False)
    )
    # Últimos 4 chars do segredo, em claro, só para o humano conferir no painel
    # que cadastrou a chave certa ("...a3f9"). Não reduz a segurança de forma útil.
    hint: Optional[str] = Field(default=None, sa_column=Column("hint", String(8), nullable=True))
    created_at: datetime = Field(
        default_factory=datetime.utcnow, sa_column=Column("created_at", nullable=False)
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow, sa_column=Column("updated_at", nullable=False)
    )
    last_used_at: Optional[datetime] = Field(
        default=None, sa_column=Column("last_used_at", nullable=True)
    )
