"""Tests for the home_assistant skill: adapter (HTTP), executor, and pipeline wiring."""

from uuid import uuid4

import httpx
import pytest

import core.skills.home_assistant.executor as _ha_executor
from core.skills.home_assistant.adapter import HomeAssistantAdapter
from core.skills.home_assistant.executor import HomeAssistantExecutor
from core.skills.home_assistant.pipeline import HomeAssistantPipeline
from core.skills.home_assistant.tools import HOME_ASSISTANT_TOOLS
from shared import ToolCall


@pytest.fixture(autouse=True)
def _fast_ha_verify(monkeypatch):
    """Zero the post-action verification poll delays so tests stay instant."""
    monkeypatch.setattr(_ha_executor, "_VERIFY_DELAYS", (0.0, 0.0, 0.0))


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


class _EventualAdapter:
    """Device confirms state ASYNCHRONOUSLY: call_service returns an empty changed
    list (the real-world bug that made the agent report a false failure), while the
    entity's actual state is whatever ``resulting_state`` says."""

    is_configured = True

    def __init__(self, resulting_state: str):
        self._state = resulting_state
        self.calls: list = []

    async def get_states(self):
        return _FAKE_STATES

    async def get_state(self, entity_id):
        return {"entity_id": entity_id, "state": self._state, "attributes": {"friendly_name": "Luz do Quarto"}}

    async def call_service(self, domain, service, data=None):
        self.calls.append((domain, service, data))
        return []  # empty changed → looked like nothing happened


@pytest.mark.asyncio
async def test_call_service_reports_success_from_real_state_despite_empty_changed():
    # "não consegui" bug: command worked but changed list is empty → must verify on.
    ex = HomeAssistantExecutor(_EventualAdapter("on"))
    out = await ex.execute(
        "ha_call_service", {"domain": "light", "service": "turn_on", "entity_id": "light.quarto"}
    )
    assert out["ok"] is True
    assert out["changed_count"] == 0
    assert out["resulting_state"] == "on"
    assert out["verified"] is True


@pytest.mark.asyncio
async def test_call_service_flags_unverified_when_state_did_not_change():
    # "diz que fez e não fez" bug: asked to turn OFF but the light stays on → verified False.
    ex = HomeAssistantExecutor(_EventualAdapter("on"))
    out = await ex.execute(
        "ha_call_service", {"domain": "light", "service": "turn_off", "entity_id": "light.quarto"}
    )
    assert out["ok"] is True
    assert out["verified"] is False
    assert out["resulting_state"] == "on"


@pytest.mark.asyncio
async def test_call_service_without_entity_id_skips_verification():
    # Scene/script with target in data → no single entity_id → no verify fields.
    ex = HomeAssistantExecutor(_EventualAdapter("on"))
    out = await ex.execute("ha_call_service", {"domain": "scene", "service": "turn_on"})
    assert out["ok"] is True
    assert "verified" not in out
    assert "resulting_state" not in out


class _ClimateAdapter:
    """Climate device: call_service returns empty changed; get_state reports the
    resulting hvac_mode + attributes (mirrors HA's async confirmation)."""

    is_configured = True

    def __init__(self, state: str, attributes: dict | None = None):
        self._state = state
        self._attrs = attributes or {}
        self.calls: list = []

    async def get_states(self):
        return _FAKE_STATES

    async def get_state(self, entity_id):
        return {"entity_id": entity_id, "state": self._state,
                "attributes": {"friendly_name": "Ar do Quarto", **self._attrs}}

    async def call_service(self, domain, service, data=None):
        self.calls.append((domain, service, data))
        return []


@pytest.mark.asyncio
async def test_climate_turn_on_verified_by_active_mode():
    # "deu erro mas funcionou": climate.turn_on → estado vira um modo (cool), não "on".
    ex = HomeAssistantExecutor(_ClimateAdapter("cool"))
    out = await ex.execute(
        "ha_call_service", {"domain": "climate", "service": "turn_on", "entity_id": "climate.quarto"}
    )
    assert out["ok"] is True
    assert out["resulting_state"] == "cool"
    assert out["verified"] is True


@pytest.mark.asyncio
async def test_climate_set_temperature_verified_by_attribute():
    ex = HomeAssistantExecutor(_ClimateAdapter("cool", {"temperature": 22}))
    out = await ex.execute(
        "ha_call_service",
        {"domain": "climate", "service": "set_temperature",
         "entity_id": "climate.quarto", "data": {"temperature": 22}},
    )
    assert out["ok"] is True
    assert out["verified"] is True
    assert out["expected"] == {"kind": "attr_eq:temperature", "target": 22}


@pytest.mark.asyncio
async def test_climate_turn_off_flags_unverified_when_still_active():
    ex = HomeAssistantExecutor(_ClimateAdapter("cool"))
    out = await ex.execute(
        "ha_call_service", {"domain": "climate", "service": "turn_off", "entity_id": "climate.quarto"}
    )
    assert out["ok"] is True
    assert out["verified"] is False  # asked off, still cool


# ── Invented entity_id guard + verify-read failure (the "desliga ela" incident) ─

class _RosterAdapter:
    """Adapter with a controllable roster (``get_states_cached``) and a pluggable
    ``get_state`` so we can simulate an invented id and a failing verify read."""

    is_configured = True

    def __init__(self, roster, get_state_impl=None):
        self._roster = roster
        self._get_state_impl = get_state_impl
        self.calls: list = []

    async def get_states_cached(self, ttl):
        return self._roster

    async def get_states(self):
        return self._roster

    async def get_state(self, entity_id):
        if self._get_state_impl is not None:
            return self._get_state_impl(entity_id)
        return {"entity_id": entity_id, "state": "off", "attributes": {}}

    async def call_service(self, domain, service, data=None):
        self.calls.append((domain, service, data))
        return []


@pytest.mark.asyncio
async def test_call_service_blocks_invented_entity_id():
    # The incident: agent guessed ``light.quarto`` (real id is light.led_bulb_w5k).
    # We must NOT POST a phantom command; bounce back to ha_list_entities instead.
    roster = [{"entity_id": "light.led_bulb_w5k", "state": "on",
               "attributes": {"friendly_name": "Luz do quarto"}}]
    fake = _RosterAdapter(roster)
    ex = HomeAssistantExecutor(fake)
    out = await ex.execute(
        "ha_call_service", {"domain": "light", "service": "turn_off", "entity_id": "light.quarto"}
    )
    assert out["ok"] is False
    assert out["unknown_entity"] is True
    assert "ha_list_entities" in out["error"]
    assert fake.calls == []  # phantom command never sent


@pytest.mark.asyncio
async def test_call_service_allows_known_entity_id():
    roster = [{"entity_id": "light.led_bulb_w5k", "state": "off", "attributes": {}}]
    fake = _RosterAdapter(
        roster, get_state_impl=lambda eid: {"entity_id": eid, "state": "off", "attributes": {}}
    )
    ex = HomeAssistantExecutor(fake)
    out = await ex.execute(
        "ha_call_service",
        {"domain": "light", "service": "turn_off", "entity_id": "light.led_bulb_w5k"},
    )
    assert out["ok"] is True
    assert "unknown_entity" not in out
    assert fake.calls and fake.calls[0][:2] == ("light", "turn_off")
    assert out["verified"] is True  # off requested, state off


@pytest.mark.asyncio
async def test_verify_read_failure_flags_unverified_not_success():
    # Entity passes the roster guard but the post-action verify read 404s → we must
    # report verified=False (unconfirmed), never a phantom success as before.
    roster = [{"entity_id": "light.led_bulb_w5k", "state": "on", "attributes": {}}]

    def _raise(entity_id):
        raise ValueError('Home Assistant rejeitou (404): {"message":"Entity not found."}')

    fake = _RosterAdapter(roster, get_state_impl=_raise)
    ex = HomeAssistantExecutor(fake)
    out = await ex.execute(
        "ha_call_service",
        {"domain": "light", "service": "turn_off", "entity_id": "light.led_bulb_w5k"},
    )
    assert out["ok"] is True            # HA accepted the POST
    assert out["verified"] is False     # but we could not confirm → NOT success
    assert out["resulting_state"] is None


@pytest.mark.asyncio
async def test_call_service_skips_validation_for_entity_list():
    # A comma-separated / group target isn't pre-validated (fail open) so bulk
    # actions still work.
    roster = [{"entity_id": "light.a", "state": "off", "attributes": {}}]
    fake = _RosterAdapter(
        roster, get_state_impl=lambda eid: {"entity_id": "light.a", "state": "on", "attributes": {}}
    )
    ex = HomeAssistantExecutor(fake)
    out = await ex.execute(
        "ha_call_service", {"domain": "light", "service": "turn_on", "entity_id": "light.a,light.b"}
    )
    assert "unknown_entity" not in out
    assert fake.calls and fake.calls[0][:2] == ("light", "turn_on")


# ── Adapter caches (snapshot injection) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_get_states_cached_hits_ha_once_within_ttl():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_FAKE_STATES)

    adapter = _adapter_with_handler(handler)
    a = await adapter.get_states_cached(60)
    b = await adapter.get_states_cached(60)
    assert a == b
    assert calls["n"] == 1  # second call served from cache
    await adapter.close()


@pytest.mark.asyncio
async def test_get_device_index_reuses_states_cache():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_FAKE_STATES)

    adapter = _adapter_with_handler(handler)
    await adapter.get_states_cached(300)        # 1 fetch
    index = await adapter.get_device_index(300)  # reuses the fresh states cache
    assert calls["n"] == 1
    assert {e["entity_id"] for e in index} == {"light.sala", "switch.tomada"}
    await adapter.close()


# ── Cognitive-core snapshot note (relevance gate) ────────────────────────────

class _SnapshotAdapter:
    is_configured = True

    def __init__(self):
        self.states_fetched = 0

    async def get_device_index(self, ttl):
        from core.skills.home_assistant.service import build_device_index
        return build_device_index([
            {"entity_id": "climate.quarto", "state": "off",
             "attributes": {"friendly_name": "Ar do Quarto", "temperature": 22}},
            {"entity_id": "light.sala", "state": "on", "attributes": {"friendly_name": "Luz da Sala"}},
        ])

    async def get_states_cached(self, ttl):
        self.states_fetched += 1
        return [
            {"entity_id": "climate.quarto", "state": "cool",
             "attributes": {"friendly_name": "Ar do Quarto", "temperature": 22, "current_temperature": 24}},
        ]


def _fake_ai_service(adapter):
    from types import SimpleNamespace
    from modules.desktop.ai_service import AIService
    self = SimpleNamespace(
        settings=SimpleNamespace(
            home_assistant_context_injection_enabled=True,
            home_assistant_device_index_ttl=300,
            home_assistant_snapshot_cache_ttl=8,
        ),
        _get_ha_adapter=lambda: adapter,
    )
    return AIService._build_home_assistant_context_note.__get__(self, AIService)


@pytest.mark.asyncio
async def test_snapshot_note_injected_for_known_device():
    adapter = _SnapshotAdapter()
    build_note = _fake_ai_service(adapter)
    note = await build_note("como está o ar-condicionado do quarto?")
    assert note is not None
    assert "Ar do Quarto" in note and "cool" in note
    assert "NUNCA responda" in note  # hard rule present
    assert adapter.states_fetched == 1


@pytest.mark.asyncio
async def test_snapshot_note_skipped_for_unknown_device():
    adapter = _SnapshotAdapter()
    build_note = _fake_ai_service(adapter)
    note = await build_note("liga o meu computador")
    assert note is None
    assert adapter.states_fetched == 0  # never touched HA states


@pytest.mark.asyncio
async def test_snapshot_note_disabled_by_flag():
    from types import SimpleNamespace
    from modules.desktop.ai_service import AIService
    adapter = _SnapshotAdapter()
    self = SimpleNamespace(
        settings=SimpleNamespace(home_assistant_context_injection_enabled=False),
        _get_ha_adapter=lambda: adapter,
    )
    build_note = AIService._build_home_assistant_context_note.__get__(self, AIService)
    assert await build_note("como está o ar do quarto?") is None


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
