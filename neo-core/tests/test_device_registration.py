"""Tests for deterministic device registration in the PKS.

Guards the fix for the chat model hallucinating a device_id/specs: identity is
written from real connection metadata, never transcribed by an LLM.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from core.pks.writer import PKSWriter
from modules.desktop.ai_service import AIService


# ── ai_service helpers ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "message,expected",
    [
        ("esse é meu MacBook Air", "MacBook Air"),
        ("lembra que esse device é um macbook pro", "MacBook Pro"),
        ("meu notebook está lento", "Notebook"),
        ("registra esse mac mini", "Mac mini"),
        ("qual o clima hoje?", None),
        ("manda um oi pra minha mãe", None),
    ],
)
def test_extract_device_label(message, expected):
    assert AIService._extract_device_label(message) == expected


def test_device_context_excludes_llm_model():
    # metadata["model"] is the LLM (deepseek-v4-flash), NOT the hardware model.
    ctx = AIService._build_current_device_context({
        "device_id": "3a1ed814-d004-4fee-845c-155a4ab88792",
        "hardware_id": "1F057B47-5303-525B-8F39-024624521E70",
        "os_platform": "macos",
        "os_arch": "aarch64",
        "client": "neo-desktop-tauri",
        "model": "deepseek-v4-flash",
    })
    assert "3a1ed814-d004-4fee-845c-155a4ab88792" in ctx
    assert "1F057B47-5303-525B-8F39-024624521E70" in ctx
    assert "aarch64" in ctx
    assert "deepseek-v4-flash" not in ctx  # the LLM model must never leak in


def test_device_context_empty_without_identity():
    assert AIService._build_current_device_context({}) == ""
    assert AIService._build_current_device_context({"os_platform": "macos"}) == ""


# ── Deterministic writer ───────────────────────────────────────────────────────

class _FakeBootstrap:
    async def ensure_workspace(self, user_id):
        return uuid4()


class _FakeSession:
    async def commit(self):
        pass


class _FakeSessionCtx:
    async def __aenter__(self):
        return _FakeSession()

    async def __aexit__(self, *exc):
        return False


class _FakeDB:
    def get_session(self):
        return _FakeSessionCtx()


def _fake_writer():
    writer = object.__new__(PKSWriter)
    writer.bootstrap = _FakeBootstrap()
    writer.db = _FakeDB()
    captured = []

    async def fake_apply(session, *, workspace_id, op, source_message_id):
        captured.append(op)
        return {"op": op.op}

    writer._apply_operation = fake_apply
    return writer, captured


@pytest.mark.asyncio
async def test_write_device_identity_is_verbatim():
    writer, captured = _fake_writer()
    real_id = "3a1ed814-d004-4fee-845c-155a4ab88792"

    result = await writer.write_device_identity(
        user_id=uuid4(),
        device_id=real_id,
        hardware_id="1F057B47-5303-525B-8F39-024624521E70",
        os_platform="macos",
        os_arch="aarch64",
        device_type="neo-desktop-tauri",
        label="MacBook Air",
        aliases=["MacBook Air", "meu computador"],
    )

    # First op creates the device entity under the human label.
    entity_ops = [op for op in captured if op.op == "upsert_entity"]
    assert len(entity_ops) == 1
    assert entity_ops[0].entity["name"] == "MacBook Air"
    assert entity_ops[0].entity["type"] == "device"
    assert entity_ops[0].entity["metadata"]["device_id"] == real_id

    # device_id is written verbatim as a field — never transcribed/hallucinated.
    field_ops = {op.field["key"]: op.field for op in captured if op.op == "upsert_field"}
    assert field_ops["device_id"]["value"] == real_id
    assert field_ops["device_id"]["value_type"] == "text"
    assert field_ops["hardware_id"]["value"] == "1F057B47-5303-525B-8F39-024624521E70"
    assert field_ops["os_platform"]["value"] == "macos"
    assert field_ops["os_arch"]["value"] == "aarch64"
    assert field_ops["device_type"]["value"] == "neo-desktop-tauri"

    assert result["device_id"] == real_id


@pytest.mark.asyncio
async def test_write_device_identity_defaults_name_and_skips_empty_fields():
    writer, captured = _fake_writer()

    await writer.write_device_identity(
        user_id=uuid4(),
        device_id="dev-1",
        hardware_id=None,
        os_platform=None,
        os_arch=None,
        device_type=None,
        label=None,
    )

    entity_ops = [op for op in captured if op.op == "upsert_entity"]
    assert entity_ops[0].entity["name"] == "Meu computador"  # default label
    field_keys = {op.field["key"] for op in captured if op.op == "upsert_field"}
    assert field_keys == {"device_id"}  # only the present field is written


@pytest.mark.asyncio
async def test_write_device_identity_noop_without_device_id():
    writer, captured = _fake_writer()
    result = await writer.write_device_identity(user_id=uuid4(), device_id="")
    assert result == {}
    assert captured == []
