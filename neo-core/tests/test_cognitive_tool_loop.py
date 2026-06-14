"""Cognitive agentic read-tool loop — the brain calls read tools inline.

Exercises `_run_cognitive_tool_loop` with a fake provider (scripted to call a tool
then answer) and a fake executor, plus `CognitiveReadExecutor` user scoping.
"""

import pytest
from uuid import uuid4

from core.cognitive import CognitiveReadExecutor, cognitive_tools_for
from modules.desktop.ai_service import AIService
from shared import ToolCall, ToolResult
from shared.types import Message, MessageRole


# ── fakes ───────────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, content="", tool_calls=None):
        self._content = content
        self._tc = tool_calls or []

    def get_content(self):
        return self._content

    def get_tool_calls(self):
        return self._tc


class _ScriptedProvider:
    """Returns a queued response per complete() call."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.tool_flags = []

    async def complete(self, *, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        self.tool_flags.append(tools is not None)
        return self._responses.pop(0)


class _FakeExecutor:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def execute_tool_call(self, tool_call):
        self.calls.append((tool_call.name, tool_call.arguments))
        return ToolResult(tool_call_id=tool_call.id, name=tool_call.name, result=self._result, execution_time=0.0)


def _svc(settings_overrides=None):
    svc = object.__new__(AIService)
    class _S:
        cognitive_max_tool_rounds = 3
        default_max_tokens = 1024
    svc.settings = _S()
    for k, v in (settings_overrides or {}).items():
        setattr(svc.settings, k, v)
    return svc


@pytest.mark.asyncio
async def test_loop_calls_tool_then_answers_from_result():
    svc = _svc()
    provider = _ScriptedProvider([
        _Resp(tool_calls=[ToolCall(id="1", name="search_pks", arguments={"query": "treino"})]),
        _Resp(content="Não achei treino no seu PKS — quer que eu salve agora?"),
    ])
    execu = _FakeExecutor({"found": False, "count": 0, "results": []})
    messages = [Message(role=MessageRole.SYSTEM, content="sys"),
                Message(role=MessageRole.USER, content="salvou mesmo?")]
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=messages, req_model=None, model_name="m",
        provider_name="p", tools=cognitive_tools_for(uuid4()), tool_executor=execu,
        slog=None, trace=None,
    )
    assert "Não achei" in out
    assert execu.calls == [("search_pks", {"query": "treino"})]
    # results were fed back as a USER message before the final answer
    assert any(m.role == MessageRole.USER and "Tool: search_pks" in (m.content or "") for m in messages)


@pytest.mark.asyncio
async def test_loop_no_tool_call_is_single_pass():
    svc = _svc()
    provider = _ScriptedProvider([_Resp(content="oi, tudo bem?")])
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="oi")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=_FakeExecutor({}), slog=None, trace=None,
    )
    assert out == "oi, tudo bem?"
    assert len(provider.tool_flags) == 1  # only one LLM call


@pytest.mark.asyncio
async def test_loop_dedups_repeated_tool_calls():
    svc = _svc()
    tc = ToolCall(id="1", name="search_pks", arguments={"query": "treino"})
    provider = _ScriptedProvider([
        _Resp(tool_calls=[tc]),
        _Resp(tool_calls=[ToolCall(id="2", name="search_pks", arguments={"query": "treino"})]),
        _Resp(content="resposta final"),
    ])
    execu = _FakeExecutor({"found": False})
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="x")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=execu, slog=None, trace=None,
    )
    assert out == "resposta final"
    assert len(execu.calls) == 1  # identical 2nd call not re-executed


@pytest.mark.asyncio
async def test_loop_respects_max_rounds():
    svc = _svc({"cognitive_max_tool_rounds": 1})
    # Always tries to call a tool; after max_rounds the last call has tools=None.
    provider = _ScriptedProvider([
        _Resp(tool_calls=[ToolCall(id="1", name="search_pks", arguments={"query": "a"})]),
        _Resp(content="forçado a responder"),
    ])
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="x")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=_FakeExecutor({}), slog=None, trace=None,
    )
    assert out == "forçado a responder"
    assert provider.tool_flags == [True, False]  # last round: no tools


# ── CognitiveReadExecutor scoping ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_read_executor_withholds_scoped_tools_without_user():
    ex = CognitiveReadExecutor(global_executor=None, db=None, pks_service=None, user_id=None)
    out = await ex.execute_tool_call(ToolCall(id="1", name="search_pks", arguments={"query": "x"}))
    assert out.error is not None  # cannot scope → error, never a wrong-user read
