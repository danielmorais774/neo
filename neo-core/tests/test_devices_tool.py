"""Tests for the `list_connected_devices` server-side tool.

Covers the WS+DB online-status merge, the `only_online` filter, per-user
scoping, the ToolSchema definition, and the DevicesContextExecutor delegation.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from modules.devices import DEVICES_TOOLS, DevicesExecutor, DevicesContextExecutor
from modules.devices.service import list_devices_with_status
from shared import ToolCall, ToolResult
from shared.types import ToolSchema


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _FakeDevice:
    """Minimal stand-in for the ConnectedDevice model."""

    def __init__(self, device_id: str, *, is_online: bool = False, user_id=None):
        self.device_id = device_id
        # `user_id` é NOT NULL no modelo real — o fake precisa ter também, senão
        # ele deixa de representar o que o código de produção sempre recebe.
        self.user_id = user_id or uuid4()
        self.device_type = "neo-desktop"
        self.model = "TestModel"
        self.hardware_id = "HW-" + device_id
        self.os_platform = "Linux"
        self.os_version = "6.0"
        self.client_version = "1.0.0"
        self.ip_address = "127.0.0.1"
        self.is_online = is_online
        self.ws_connection_id = None
        self.created_at = datetime(2026, 6, 4, tzinfo=timezone.utc)


class _FakeDB:
    """Fake AsyncPostgresClient capturing the user_id passed to the query."""

    def __init__(self, devices):
        self._devices = devices
        self.called_with_user_id = "UNSET"

    async def get_connected_devices(self, user_id=None):
        self.called_with_user_id = user_id
        if user_id is None:
            return list(self._devices)
        return [d for d in self._devices if getattr(d, "_user_id", user_id) == user_id]


def _install_fake_ws_registry(monkeypatch, online_ids):
    """Inject a fake `grpc_server` module so the late import resolves to it."""
    registry = types.SimpleNamespace()

    async def list_online():
        return list(online_ids)

    registry.list_online = list_online
    module = types.ModuleType("grpc_server")
    module._devices_ws_registry = registry
    monkeypatch.setitem(sys.modules, "grpc_server", module)


# ── 1. WS+DB merge ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ws_registry_overrides_db_online_status(monkeypatch):
    # dev-a: offline in DB but present in WS registry → online
    # dev-b: offline in DB and absent from registry → stays offline
    devices = [_FakeDevice("dev-a", is_online=False), _FakeDevice("dev-b", is_online=False)]
    _install_fake_ws_registry(monkeypatch, online_ids=["dev-a"])

    result = await list_devices_with_status(_FakeDB(devices), user_id=None)

    by_id = {d["device_id"]: d for d in result}
    assert by_id["dev-a"]["is_online"] is True
    assert by_id["dev-b"]["is_online"] is False


@pytest.mark.asyncio
async def test_db_online_trusted_when_absent_from_registry(monkeypatch):
    # dev-c: online in DB, no active WS connection → trust DB (online)
    devices = [_FakeDevice("dev-c", is_online=True)]
    _install_fake_ws_registry(monkeypatch, online_ids=[])

    result = await list_devices_with_status(_FakeDB(devices), user_id=None)

    assert result[0]["is_online"] is True


# ── 2. only_online filter ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_only_online_filter(monkeypatch):
    devices = [_FakeDevice("dev-a", is_online=False), _FakeDevice("dev-b", is_online=False)]
    _install_fake_ws_registry(monkeypatch, online_ids=["dev-a"])

    result = await list_devices_with_status(_FakeDB(devices), user_id=None, only_online=True)

    assert [d["device_id"] for d in result] == ["dev-a"]


# ── 3. Per-user scoping ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_executor_scopes_to_user(monkeypatch):
    _install_fake_ws_registry(monkeypatch, online_ids=[])
    db = _FakeDB([_FakeDevice("dev-a")])
    user_id = uuid4()

    executor = DevicesExecutor(db=db)
    executor.set_context(user_id)
    out = await executor.execute("list_connected_devices", {})

    assert db.called_with_user_id == user_id
    assert out["count"] == 1
    assert out["devices"][0]["device_id"] == "dev-a"


@pytest.mark.asyncio
async def test_executor_without_context_lists_all(monkeypatch):
    _install_fake_ws_registry(monkeypatch, online_ids=[])
    db = _FakeDB([_FakeDevice("dev-a")])

    executor = DevicesExecutor(db=db)
    await executor.execute("list_connected_devices", {})

    assert db.called_with_user_id is None


@pytest.mark.asyncio
async def test_executor_rejects_unknown_tool():
    executor = DevicesExecutor(db=_FakeDB([]))
    with pytest.raises(ValueError):
        await executor.execute("not_a_real_tool", {})


# ── 4. ToolSchema validity ─────────────────────────────────────────────────────

def test_tool_schema_definition():
    tool = next(t for t in DEVICES_TOOLS if t.name == "list_connected_devices")
    assert isinstance(tool, ToolSchema)
    assert tool.execution == "server-side"
    assert tool.module == "devices"
    assert "only_online" in tool.parameters
    assert tool.parameters["only_online"].required is False


def test_tool_validator_accepts_arguments():
    from core.tools.validator import ToolValidator

    tool = next(t for t in DEVICES_TOOLS if t.name == "list_connected_devices")
    validator = ToolValidator()
    assert validator.validate_arguments(tool, {}) == {}
    validated = validator.validate_arguments(tool, {"only_online": True})
    assert validated["only_online"] is True


# ── 5. DevicesContextExecutor delegation ───────────────────────────────────────

class _SpyGlobalExecutor:
    def __init__(self):
        self.delegated = None

    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        self.delegated = tool_call.name
        return ToolResult(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            result={"ok": True},
            execution_time=0.0,
        )


@pytest.mark.asyncio
async def test_context_executor_delegates_non_device_tool():
    spy = _SpyGlobalExecutor()
    ctx = DevicesContextExecutor(
        global_executor=spy,
        devices_executor=DevicesExecutor(db=_FakeDB([])),
        user_id=uuid4(),
    )

    result = await ctx.execute_tool_call(ToolCall(name="get_weather", arguments={"city": "X"}))

    assert spy.delegated == "get_weather"
    assert result.result == {"ok": True}


@pytest.mark.asyncio
async def test_context_executor_handles_device_tool(monkeypatch):
    _install_fake_ws_registry(monkeypatch, online_ids=["dev-a"])
    spy = _SpyGlobalExecutor()
    db = _FakeDB([_FakeDevice("dev-a")])
    user_id = uuid4()

    ctx = DevicesContextExecutor(
        global_executor=spy,
        devices_executor=DevicesExecutor(db=db),
        user_id=user_id,
    )

    result = await ctx.execute_tool_call(
        ToolCall(name="list_connected_devices", arguments={})
    )

    assert spy.delegated is None  # not delegated
    assert db.called_with_user_id == user_id
    assert result.error is None
    assert result.result["count"] == 1
