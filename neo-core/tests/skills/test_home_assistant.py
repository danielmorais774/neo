"""Tests for the home_assistant skill: adapter (HTTP), executor, and pipeline wiring."""

from uuid import uuid4

import httpx
import pytest

from core.skills.home_assistant.adapter import HomeAssistantAdapter
from core.skills.home_assistant.executor import HomeAssistantExecutor
from core.skills.home_assistant.pipeline import HomeAssistantPipeline
from core.skills.home_assistant.tools import HOME_ASSISTANT_TOOLS
from shared import ToolCall


_FAKE_STATES = [
    {"entity_id": "light.sala", "state": "off", "attributes": {"friendly_name": "Luz da Sala"}},
    {"entity_id": "switch.tomada", "state": "on", "attributes": {"friendly_name": "Tomada"}},
]


def _adapter_with_handler(handler) -> HomeAssistantAdapter:
    """Build an adapter wired to an httpx MockTransport handler."""
    adapter = HomeAssistantAdapter()
    adapter._token = "test-token"
    adapter.base_url = "http://ha.test"
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ha.test",
        headers={"Authorization": "Bearer test-token"},
    )
    return adapter


# ── Adapter HTTP behavior ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_adapter_get_states():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/states"
        return httpx.Response(200, json=_FAKE_STATES)

    adapter = _adapter_with_handler(handler)
    states = await adapter.get_states()
    assert len(states) == 2
    await adapter.close()


@pytest.mark.asyncio
async def test_adapter_call_service_posts_payload():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/services/light/turn_on"
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=[{"entity_id": "light.sala", "state": "on", "attributes": {}}])

    adapter = _adapter_with_handler(handler)
    changed = await adapter.call_service("light", "turn_on", {"entity_id": "light.sala", "brightness_pct": 50})
    assert seen["body"]["entity_id"] == "light.sala"
    assert seen["body"]["brightness_pct"] == 50
    assert changed[0]["entity_id"] == "light.sala"
    await adapter.close()


@pytest.mark.asyncio
async def test_adapter_4xx_raises_valueerror_no_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="not found")

    adapter = _adapter_with_handler(handler)
    with pytest.raises(ValueError):
        await adapter.get_state("light.inexistente")
    assert calls["n"] == 1  # no retry on 4xx
    await adapter.close()


@pytest.mark.asyncio
async def test_adapter_not_configured_blocks_calls():
    adapter = HomeAssistantAdapter()
    adapter._token = None
    assert adapter.is_configured is False
    with pytest.raises(Exception):
        await adapter.get_states()


# ── Executor dispatch ────────────────────────────────────────────────────────

class _FakeAdapter:
    is_configured = True

    def __init__(self):
        self.calls = []

    async def get_states(self):
        return _FAKE_STATES

    async def get_state(self, entity_id):
        return {"entity_id": entity_id, "state": "on", "attributes": {"friendly_name": "Luz da Sala"}}

    async def call_service(self, domain, service, data=None):
        self.calls.append((domain, service, data))
        return [{"entity_id": (data or {}).get("entity_id", ""), "state": "on", "attributes": {}}]

    async def rename_entity(self, entity_id, name):
        self.calls.append(("rename", entity_id, name))
        return {"entity_id": entity_id, "name": name}


@pytest.mark.asyncio
async def test_executor_list_entities_filters():
    ex = HomeAssistantExecutor(_FakeAdapter())
    out = await ex.execute("ha_list_entities", {"domain": "light"})
    assert out["count"] == 1
    assert out["entities"][0]["entity_id"] == "light.sala"


@pytest.mark.asyncio
async def test_executor_call_service_merges_entity_id():
    fake = _FakeAdapter()
    ex = HomeAssistantExecutor(fake)
    out = await ex.execute(
        "ha_call_service",
        {"domain": "light", "service": "turn_on", "entity_id": "light.sala", "data": {"brightness_pct": 50}},
    )
    assert out["ok"] is True
    assert fake.calls == [("light", "turn_on", {"brightness_pct": 50, "entity_id": "light.sala"})]


@pytest.mark.asyncio
async def test_executor_rename_entity_dispatch():
    fake = _FakeAdapter()
    ex = HomeAssistantExecutor(fake)
    out = await ex.execute("ha_rename_entity", {"entity_id": "light.sala", "name": "Luz Principal"})
    assert out["ok"] is True
    assert out["new_name"] == "Luz Principal"
    assert ("rename", "light.sala", "Luz Principal") in fake.calls


@pytest.mark.asyncio
async def test_executor_rename_empty_name_shows_default_label():
    ex = HomeAssistantExecutor(_FakeAdapter())
    out = await ex.execute("ha_rename_entity", {"entity_id": "light.sala", "name": ""})
    assert out["ok"] is True
    assert "padrão" in out["new_name"]


@pytest.mark.asyncio
async def test_executor_tool_call_captures_errors():
    ex = HomeAssistantExecutor(_FakeAdapter())
    result = await ex.execute_tool_call(ToolCall(id="1", name="ha_unknown", arguments={}))
    assert result.error is not None


# ── Pipeline wiring ──────────────────────────────────────────────────────────

class _FakeAgentExecutor:
    """Drives the supplied tool_executor once, then returns a final response."""

    def __init__(self):
        self.received = {}

    async def execute(self, task, session_id=None, memory=None, system_prompt_override=None,
                       tool_subset=None, tool_executor=None, available_tools=None, **kwargs):
        self.received = {
            "task": task,
            "system_prompt": system_prompt_override,
            "tools": [t.name for t in (available_tools or [])],
        }
        # Simulate the agent calling ha_call_service via the injected executor.
        await tool_executor.execute_tool_call(
            ToolCall(id="x", name="ha_call_service",
                     arguments={"domain": "light", "service": "turn_on", "entity_id": "light.sala"})
        )
        return {"status": "completed", "final_response": "Pronto, acendi a luz da sala."}


# ── Adapter WebSocket rename (real local WS server mimicking HA) ──────────────

@pytest.mark.asyncio
async def test_adapter_rename_entity_ws_handshake():
    import asyncio
    import json
    import websockets

    received = {}

    async def ha_server(ws):
        await ws.send(json.dumps({"type": "auth_required", "ha_version": "test"}))
        auth = json.loads(await ws.recv())
        received["token"] = auth.get("access_token")
        await ws.send(json.dumps({"type": "auth_ok"}))
        cmd = json.loads(await ws.recv())
        received["cmd"] = cmd
        await ws.send(json.dumps({
            "id": cmd["id"], "type": "result", "success": True,
            "result": {"entity_entry": {"entity_id": cmd["entity_id"], "name": cmd["name"]}},
        }))

    async with websockets.serve(ha_server, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = HomeAssistantAdapter()
        adapter._token = "tok-123"
        adapter.base_url = f"http://127.0.0.1:{port}"
        adapter._timeout = 5

        entry = await adapter.rename_entity("light.sala", "Luz Principal")

    assert received["token"] == "tok-123"
    assert received["cmd"]["type"] == "config/entity_registry/update"
    assert received["cmd"]["entity_id"] == "light.sala"
    assert received["cmd"]["name"] == "Luz Principal"
    assert entry["name"] == "Luz Principal"


@pytest.mark.asyncio
async def test_adapter_rename_entity_ws_rejects_on_failure():
    import json
    import websockets

    async def ha_server(ws):
        await ws.send(json.dumps({"type": "auth_required"}))
        await ws.recv()
        await ws.send(json.dumps({"type": "auth_ok"}))
        cmd = json.loads(await ws.recv())
        await ws.send(json.dumps({
            "id": cmd["id"], "type": "result", "success": False,
            "error": {"code": "not_found", "message": "Entity not found"},
        }))

    async with websockets.serve(ha_server, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = HomeAssistantAdapter()
        adapter._token = "tok"
        adapter.base_url = f"http://127.0.0.1:{port}"
        adapter._timeout = 5
        with pytest.raises(ValueError, match="recusou"):
            await adapter.rename_entity("light.inexistente", "X")


@pytest.mark.asyncio
async def test_pipeline_runs_and_returns_skill_result(monkeypatch):
    fake_adapter = _FakeAdapter()
    fake_agent = _FakeAgentExecutor()

    pipeline = HomeAssistantPipeline(agent_executor=fake_agent, db=None, broker=None)
    # Inject the fake adapter (is_configured=True) into the pipeline + executor.
    pipeline.adapter = fake_adapter
    pipeline.executor = HomeAssistantExecutor(fake_adapter)

    result = await pipeline.run(
        session_id=uuid4(),
        user_message="acende a luz da sala",
        history=[],
    )

    assert result.ok is True
    assert "luz da sala" in result.message.lower()
    # Pipeline must hand the HA tools + prompt to the agent.
    assert set(fake_agent.received["tools"]) == {t.name for t in HOME_ASSISTANT_TOOLS}
    assert fake_agent.received["system_prompt"]
    # And the simulated tool call reached the adapter.
    assert fake_adapter.calls and fake_adapter.calls[0][:2] == ("light", "turn_on")


@pytest.mark.asyncio
async def test_pipeline_not_configured_returns_friendly_error():
    pipeline = HomeAssistantPipeline(agent_executor=_FakeAgentExecutor(), db=None, broker=None)

    class _Unconfigured(_FakeAdapter):
        @property
        def is_configured(self):
            return False

    pipeline.adapter = _Unconfigured()
    result = await pipeline.run(session_id=uuid4(), user_message="acende a luz", history=[])
    assert result.ok is False
    assert "configurado" in result.message.lower()
