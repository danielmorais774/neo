"""Tool gating no _run_cognitive_tool_loop: skill_read destrava as tools da skill.

Uma skill tool_backed (ex. postgres-neo → sql_query_ro) não paga o schema das
suas tools no prompt: elas só entram no toolset dos rounds seguintes ao
skill_read — e somente neste turno. Ver docs/skills-library-plan.md §4.4.
"""

from types import SimpleNamespace

import pytest

from modules.desktop.ai_service import AIService
from shared import ToolCall, ToolResult
from shared.types import Message, MessageRole, ToolSchema


class _Resp:
    def __init__(self, content="", tool_calls=None):
        self._content = content
        self._tc = tool_calls or []

    def get_content(self):
        return self._content

    def get_tool_calls(self):
        return self._tc


class _ScriptedProvider:
    def __init__(self, responses):
        self._responses = list(responses)
        self.toolsets = []  # nomes das tools oferecidas em cada round

    # `**kwargs` porque o loop passa a intenção `disable_thinking` (e pode
    # passar `tool_choice`): o dublê ignora, o provider real traduz.
    async def complete(
        self, *, messages, tools=None, model=None, temperature=0.7,
        max_tokens=4096, **kwargs,
    ):
        self.toolsets.append([t.name for t in (tools or [])])
        return self._responses.pop(0)


class _FakeExecutor:
    def __init__(self):
        self.calls = []

    async def execute_tool_call(self, tool_call):
        self.calls.append(tool_call.name)
        return ToolResult(
            tool_call_id=tool_call.id, name=tool_call.name,
            result="# SKILL: postgres-neo (tool_backed)\n..." if tool_call.name == "skill_read" else "(1 linha)\ncount\n42",
            execution_time=0.0,
        )


class _FakeLibrary:
    """tools_for devolve sql_query_ro para postgres-neo, nada para o resto."""

    def __init__(self):
        self.sql_tool = ToolSchema(
            name="sql_query_ro", description="select ro", module="sqlro",
            execution="server-side", parameters={},
        )

    def tools_for(self, name):
        return [self.sql_tool] if name == "postgres-neo" else []


def _svc(with_library=True):
    svc = object.__new__(AIService)
    svc.settings = SimpleNamespace(
        cognitive_max_tool_rounds=3,
        default_max_tokens=1024,
        cognitive_stream_enabled=False,
        cognitive_reasoning_enabled=False,
    )
    if with_library:
        svc.skill_library = _FakeLibrary()
    return svc


def _skill_read_tool():
    return ToolSchema(
        name="skill_read", description="lê skill", module="skills_library",
        execution="server-side", parameters={},
    )


@pytest.mark.asyncio
async def test_skill_read_unlocks_gated_tool():
    svc = _svc()
    provider = _ScriptedProvider([
        _Resp(tool_calls=[ToolCall(id="1", name="skill_read", arguments={"name": "postgres-neo"})]),
        _Resp(tool_calls=[ToolCall(id="2", name="sql_query_ro", arguments={"query": "SELECT count(*) FROM x"})]),
        _Resp(content="Você tem 42."),
    ])
    execu = _FakeExecutor()
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="quantas?")],
        req_model=None, model_name="m", provider_name="p",
        tools=[_skill_read_tool()], tool_executor=execu, slog=None, trace=None,
    )
    assert out == "Você tem 42."
    assert execu.calls == ["skill_read", "sql_query_ro"]
    # round 1: só skill_read; rounds seguintes: + sql_query_ro
    assert provider.toolsets[0] == ["skill_read"]
    assert "sql_query_ro" in provider.toolsets[1]


@pytest.mark.asyncio
async def test_unknown_skill_unlocks_nothing():
    svc = _svc()
    provider = _ScriptedProvider([
        _Resp(tool_calls=[ToolCall(id="1", name="skill_read", arguments={"name": "nao-existe"})]),
        _Resp(content="ok"),
    ])
    await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="x")],
        req_model=None, model_name="m", provider_name="p",
        tools=[_skill_read_tool()], tool_executor=_FakeExecutor(), slog=None, trace=None,
    )
    assert provider.toolsets[1] == ["skill_read"]  # nada destravado


@pytest.mark.asyncio
async def test_loop_without_library_attr_still_works():
    # AIServices antigos (object.__new__ em outros testes) não têm skill_library.
    svc = _svc(with_library=False)
    provider = _ScriptedProvider([
        _Resp(tool_calls=[ToolCall(id="1", name="skill_read", arguments={"name": "postgres-neo"})]),
        _Resp(content="ok"),
    ])
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="x")],
        req_model=None, model_name="m", provider_name="p",
        tools=[_skill_read_tool()], tool_executor=_FakeExecutor(), slog=None, trace=None,
    )
    assert out == "ok"
