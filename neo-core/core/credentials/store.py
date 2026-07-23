"""CredentialStore — cofre Fernet do Neo (docs/service-gateway-dsl-plan.md §2.2).

Store ÚNICO, compartilhado com o browser-agent (browser-agent-plan §3.5): os dois
referenciam a mesma linha por ``fernet://<id>``.

Contrato com o resto do sistema:
- ``get_secret(id_or_label)`` é o ÚNICO caminho que devolve o valor em claro, e
  existe para o CredentialBroker chamar no momento da execução.
- Todo o resto (list/create/update) trafega em ``CredentialInfo`` — rótulo,
  tipo, hint (últimos 4 chars). O painel nunca recebe o segredo de volta: uma vez
  cadastrado, só dá para SUBSTITUIR, não para ler. É de propósito.

``cryptography`` é importada no topo porque é requisito real deste módulo; quem
o importa é o bootstrap, e SÓ quando ``NEO_CRED_KEY`` está configurada — assim
um ambiente sem a chave (ou sem a lib) nunca quebra no import.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlmodel import select

from db.models.credential import CREDENTIAL_KINDS, Credential

logger = logging.getLogger(__name__)


class CredentialError(Exception):
    """Erro de negócio do cofre (chave ausente/errada, rótulo duplicado)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class CredentialInfo:
    """Vista PÚBLICA de uma credencial — nunca carrega o segredo."""

    id: str
    label: str
    kind: str
    description: str | None
    hint: str | None
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None

    @property
    def ref(self) -> str:
        """A referência que vai na conexão (``credential_ref``)."""
        return f"fernet://{self.id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "description": self.description,
            "hint": self.hint,
            "ref": self.ref,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
        }


def generate_key() -> str:
    """Gera uma NEO_CRED_KEY nova (para o operador colar na env)."""
    return Fernet.generate_key().decode()


class CredentialStore:
    """Cofre cifrado. A chave vem da env; o banco só vê ciphertext."""

    def __init__(self, *, db: Any, key: str) -> None:
        if not key:
            raise CredentialError("no_key", "NEO_CRED_KEY não configurada.")
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            raise CredentialError(
                "bad_key",
                "NEO_CRED_KEY inválida (precisa ser uma chave Fernet base64 de 32 bytes — "
                "gere com core.credentials.generate_key()).",
            ) from exc
        self._db = db

    # ------------------------------------------------------------------
    # Escrita
    # ------------------------------------------------------------------

    async def create(
        self, *, label: str, secret: str, kind: str = "token", description: str | None = None
    ) -> CredentialInfo:
        label = (label or "").strip()
        if not label:
            raise CredentialError("bad_label", "rótulo é obrigatório.")
        if kind not in CREDENTIAL_KINDS:
            raise CredentialError("bad_kind", f"tipo inválido: {kind!r}")
        if not secret:
            raise CredentialError("empty_secret", "segredo vazio.")
        if await self._find_by_label(label) is not None:
            raise CredentialError("duplicate_label", f"já existe uma credencial '{label}'.")
        entry = Credential(
            label=label,
            kind=kind,
            description=description,
            value_encrypted=self._fernet.encrypt(secret.encode()),
            hint=secret[-4:] if len(secret) >= 8 else None,
        )
        async with self._db.get_session() as session:
            session.add(entry)
            await session.commit()
            await session.refresh(entry)
        logger.info("[credentials] criada '%s' (%s)", label, kind)
        return self._info(entry)

    async def update(
        self,
        credential_id: UUID | str,
        *,
        secret: str | None = None,
        label: str | None = None,
        description: str | None = None,
        kind: str | None = None,
    ) -> CredentialInfo:
        """Atualiza. ``secret`` novo SUBSTITUI o antigo (não há leitura do velho)."""
        entry = await self._get_row(credential_id)
        if entry is None:
            raise CredentialError("not_found", "credencial não encontrada.")
        if label and label.strip() != entry.label:
            other = await self._find_by_label(label.strip())
            if other is not None:
                raise CredentialError("duplicate_label", f"já existe uma credencial '{label}'.")
            entry.label = label.strip()
        if kind is not None:
            if kind not in CREDENTIAL_KINDS:
                raise CredentialError("bad_kind", f"tipo inválido: {kind!r}")
            entry.kind = kind
        if description is not None:
            entry.description = description
        if secret:
            entry.value_encrypted = self._fernet.encrypt(secret.encode())
            entry.hint = secret[-4:] if len(secret) >= 8 else None
        entry.updated_at = datetime.utcnow()
        async with self._db.get_session() as session:
            session.add(entry)
            await session.commit()
            await session.refresh(entry)
        return self._info(entry)

    async def delete(self, credential_id: UUID | str) -> bool:
        entry = await self._get_row(credential_id)
        if entry is None:
            return False
        async with self._db.get_session() as session:
            await session.delete(entry)
            await session.commit()
        logger.info("[credentials] removida '%s'", entry.label)
        return True

    # ------------------------------------------------------------------
    # Leitura
    # ------------------------------------------------------------------

    async def list(self) -> list[CredentialInfo]:
        async with self._db.get_session() as session:
            result = await session.execute(select(Credential).order_by(Credential.label))
            return [self._info(row) for row in result.scalars().all()]

    async def get_info(self, credential_id: UUID | str) -> CredentialInfo | None:
        entry = await self._get_row(credential_id)
        return self._info(entry) if entry else None

    async def get_secret(self, id_or_label: str) -> str | None:
        """Decifra o segredo. ÚNICO ponto que devolve valor em claro.

        Aceita id (uuid) ou rótulo — o painel cadastra por rótulo, e a conexão
        referencia por id; deixar os dois funcionarem evita um round-trip.
        """
        entry = await self._get_row(id_or_label)
        if entry is None:
            entry = await self._find_by_label(str(id_or_label))
        if entry is None:
            return None
        return self._decrypt(entry)

    def _decrypt(self, entry: Credential) -> str:
        try:
            return self._fernet.decrypt(entry.value_encrypted).decode()
        except InvalidToken as exc:
            raise CredentialError(
                "decrypt_failed",
                f"não consegui decifrar '{entry.label}': a NEO_CRED_KEY mudou? "
                "(re-cadastre a credencial com a chave atual)",
            ) from exc

    async def touch(self, credential_id: UUID | str) -> None:
        try:
            entry = await self._get_row(credential_id)
            if entry is None:
                return
            entry.last_used_at = datetime.utcnow()
            async with self._db.get_session() as session:
                session.add(entry)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 — telemetria não derruba a chamada
            logger.debug("[credentials] touch falhou: %s", exc)

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    async def _get_row(self, credential_id: UUID | str) -> Credential | None:
        cid = credential_id if isinstance(credential_id, UUID) else self._as_uuid(credential_id)
        if cid is None:
            return None
        async with self._db.get_session() as session:
            return await session.get(Credential, cid)

    async def _find_by_label(self, label: str) -> Credential | None:
        async with self._db.get_session() as session:
            result = await session.execute(select(Credential).where(Credential.label == label))
            return result.scalars().first()

    @staticmethod
    def _as_uuid(value: str) -> UUID | None:
        try:
            return UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            return None

    @staticmethod
    def _info(entry: Credential) -> CredentialInfo:
        return CredentialInfo(
            id=str(entry.id),
            label=entry.label,
            kind=entry.kind,
            description=entry.description,
            hint=entry.hint,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
            last_used_at=entry.last_used_at,
        )
