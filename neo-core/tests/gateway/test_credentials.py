"""Cofre Fernet + guard do PKS (F2 — docs/service-gateway-dsl-plan.md §2.2).

Duas garantias que importam aqui:
1. O banco só vê ciphertext; sem a NEO_CRED_KEY certa, o valor não sai.
2. Segredo NUNCA vira memória semântica (o PKS é injetado no prompt).
"""

from __future__ import annotations

import pytest

from core.credentials import build_credential_store
from core.credentials.guard import (
    credential_violation,
    is_secret_key,
    looks_like_secret_value,
)
from core.credentials.store import CredentialError, CredentialStore, generate_key
from db.models.credential import Credential


class _FakeSession:
    def __init__(self, db) -> None:
        self._db = db

    def add(self, entry) -> None:
        self._db.rows[entry.id] = entry

    async def commit(self) -> None:
        return None

    async def refresh(self, entry) -> None:
        return None

    async def get(self, model, pk):
        return self._db.rows.get(pk)

    async def delete(self, entry) -> None:
        self._db.rows.pop(entry.id, None)

    async def execute(self, stmt):
        return _Result(list(self._db.rows.values()), self._db.label_filter)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Result:
    def __init__(self, rows, label_filter=None) -> None:
        self._rows = [r for r in rows if label_filter is None or r.label == label_filter]

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self) -> None:
        self.rows: dict = {}
        self.label_filter = None

    def get_session(self):
        return _FakeSession(self)


@pytest.fixture
def store():
    return CredentialStore(db=_FakeDB(), key=generate_key())


# ── chave ───────────────────────────────────────────────────────────────────

def test_generate_key_roundtrips():
    key = generate_key()
    s = CredentialStore(db=_FakeDB(), key=key)
    assert s is not None


def test_bad_key_is_rejected_with_actionable_message():
    with pytest.raises(CredentialError) as e:
        CredentialStore(db=_FakeDB(), key="chave-qualquer")
    assert e.value.code == "bad_key"
    assert "Fernet" in e.value.message


def test_build_store_without_key_returns_none():
    # Sem NEO_CRED_KEY o app não quebra: fica sem cofre (só env:// funciona).
    assert build_credential_store(db=_FakeDB(), key="") is None


# ── cifra ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_secret_is_encrypted_at_rest_and_readable_back(store):
    info = await store.create(label="github", secret="ghp_supersecrettoken123456", kind="token")
    row = list(store._db.rows.values())[0]  # noqa: SLF001 — inspeção do que foi gravado
    # O que vai pro banco NÃO é o segredo.
    assert b"ghp_supersecrettoken123456" not in row.value_encrypted
    assert isinstance(row.value_encrypted, bytes)
    # Mas o cofre lê de volta.
    assert await store.get_secret(info.id) == "ghp_supersecrettoken123456"


@pytest.mark.asyncio
async def test_wrong_key_cannot_decrypt(store):
    info = await store.create(label="x", secret="segredo-do-usuario-123")
    other = CredentialStore(db=store._db, key=generate_key())  # noqa: SLF001
    with pytest.raises(CredentialError) as e:
        await other.get_secret(info.id)
    assert e.value.code == "decrypt_failed"
    assert "NEO_CRED_KEY" in e.value.message  # diz o que houve


@pytest.mark.asyncio
async def test_info_never_carries_secret(store):
    info = await store.create(label="github", secret="ghp_abcdefgh12345678")
    payload = info.to_dict()
    assert "ghp_abcdefgh12345678" not in str(payload)
    assert payload["hint"] == "5678"          # só os últimos 4, p/ conferência humana
    assert payload["ref"] == f"fernet://{info.id}"


@pytest.mark.asyncio
async def test_short_secret_has_no_hint(store):
    info = await store.create(label="x", secret="curto")
    assert info.hint is None


@pytest.mark.asyncio
async def test_duplicate_label_rejected(store):
    await store.create(label="github", secret="aaaaaaaaaaaa")
    store._db.label_filter = "github"  # noqa: SLF001 — o fake não interpreta o WHERE
    with pytest.raises(CredentialError) as e:
        await store.create(label="github", secret="bbbbbbbbbbbb")
    assert e.value.code == "duplicate_label"


@pytest.mark.asyncio
async def test_update_replaces_secret(store):
    info = await store.create(label="x", secret="antigo-secreto-123")
    await store.update(info.id, secret="novo-secreto-456")
    assert await store.get_secret(info.id) == "novo-secreto-456"


@pytest.mark.asyncio
async def test_empty_secret_and_bad_kind(store):
    with pytest.raises(CredentialError):
        await store.create(label="x", secret="")
    with pytest.raises(CredentialError):
        await store.create(label="y", secret="abc123456789", kind="inventado")


@pytest.mark.asyncio
async def test_get_secret_unknown_returns_none(store):
    assert await store.get_secret("00000000-0000-0000-0000-000000000000") is None


# ── guard do PKS ────────────────────────────────────────────────────────────

def test_is_secret_key_catches_pt_and_en():
    for key in ("token", "api_key", "senha", "password", "credencial", "client_secret",
                "Authorization", "refresh token", "chave privada"):
        assert is_secret_key(key), key
    for key in ("nome", "cidade", "entity_id", "cor favorita"):
        assert not is_secret_key(key)


def test_looks_like_secret_value():
    assert looks_like_secret_value("ghp_ABCDEFGHIJKLMNOPQRSTUV123456")
    assert looks_like_secret_value("Bearer abcdefghijklmnop1234567890")
    assert looks_like_secret_value("-----BEGIN PRIVATE KEY-----\nabc")
    assert not looks_like_secret_value("mora em Campinas")
    assert not looks_like_secret_value("curto")


class _Op:
    def __init__(self, field) -> None:
        self.field = field
        self.entity = {}
        self.confidence = 1.0


def test_credential_violation_blocks_secret_key():
    op = _Op({"key": "api_key", "value_text": "xyz"})
    assert credential_violation(op) == "credential_field:api_key"


def test_credential_violation_blocks_secret_value_under_innocent_key():
    # O planner adora salvar "detalhe: ghp_..." — a chave engana, o valor não.
    op = _Op({"key": "detalhe", "value_text": "ghp_ABCDEFGHIJKLMNOPQRSTUV123456"})
    assert credential_violation(op) == "credential_value"


def test_credential_violation_allows_normal_memory():
    op = _Op({"key": "cidade", "value_text": "Campinas"})
    assert credential_violation(op) is None
    # O FATO sobre a credencial pode virar memória — o VALOR não.
    op2 = _Op({"key": "servico_favorito", "value_text": "usa a credencial github-personal"})
    assert credential_violation(op2) is None
