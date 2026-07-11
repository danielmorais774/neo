"""Integração da biblioteca de skills: fluxo real de ponta a ponta (sem DB/LLM).

Monta o encanamento REAL — ToolRegistry + ToolExecutor globais + SkillLibrary
sobre a pasta skills/ do repo + SkillsLibraryExecutor — e roda o
_run_cognitive_tool_loop com um provider roteirizado:

    round 1: skill_read("postgres-neo")   → corpo real da skill via executor real
    round 2: (gating destravou sql_query_ro — schema real vindo do registry)
    resposta final.

É o mesmo caminho do runtime, só sem Postgres (a query nunca é executada) e sem
LLM (provider fake).
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.skills.library import SkillLibrary
from core.tools.executor import ToolExecutor
from core.tools.registry import ToolRegistry
from modules.desktop.ai_service import AIService
from modules.skills_library import SKILLS_LIBRARY_TOOLS, SkillsLibraryExecutor
from modules.sqlro import SQLRO_TOOLS
from shared import ToolCall
from shared.types import Message, MessageRole

SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"


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
        self.toolsets = []

    async def complete(self, *, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        self.toolsets.append({t.name for t in (tools or [])})
        return self._responses.pop(0)


def _wire():
    """Registry + executor + library reais, como o api/main.py faz no boot."""
    settings = SimpleNamespace(
        workbench_enabled=False,  # analise-csv (scripted) deve ficar FORA do catálogo
        skills_library_enabled=True,
        skill_read_max_bytes=16_384,
        skills_catalog_max_entries=30,
        skills_router_note_enabled=True,
        tool_timeout=30,
    )
    registry = ToolRegistry()
    registry.register_many(SKILLS_LIBRARY_TOOLS)
    registry.register_many(SQLRO_TOOLS)
    executor = ToolExecutor(registry)
    executor.settings = settings  # evita get_settings() real no timeout
    library = SkillLibrary(root=SKILLS_ROOT, settings=settings, tool_registry=registry)
    library.load()
    executor.register_module_executor("skills_library", SkillsLibraryExecutor(library))
    return settings, registry, executor, library


def test_repo_skills_load_and_catalog():
    settings, _, _, library = _wire()
    names = {e.name for e in library.all()}
    assert "postgres-neo" in names and "diagnostico-voz" in names
    assert "analise-csv" not in names  # scripted, workbench off
    note = library.catalog_note("quantas conversas eu tive?")
    assert "- postgres-neo — " in note
    router_note = library.router_note()
    assert "postgres-neo" in router_note and "DIRECT_RESPONSE" in router_note


@pytest.mark.asyncio
async def test_full_flow_skill_read_unlocks_real_sql_tool():
    settings, registry, tool_executor, library = _wire()

    svc = object.__new__(AIService)
    svc.settings = SimpleNamespace(cognitive_max_tool_rounds=3, default_max_tokens=1024)
    svc.skill_library = library

    provider = _ScriptedProvider([
        _Resp(tool_calls=[ToolCall(id="1", name="skill_read", arguments={"name": "postgres-neo"})]),
        _Resp(content="Fechado: 12 sessões este mês."),
    ])
    messages = [Message(role=MessageRole.USER, content="quantas sessões esse mês?")]
    skill_read_schema = next(t for t in SKILLS_LIBRARY_TOOLS if t.name == "skill_read")

    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=messages, req_model=None, model_name="m",
        provider_name="p", tools=[skill_read_schema], tool_executor=tool_executor,
        slog=None, trace=None,
    )

    assert out == "Fechado: 12 sessões este mês."
    # round 1: só skill_read; round 2: sql_query_ro destravada (schema REAL do registry)
    assert provider.toolsets[0] == {"skill_read"}
    assert provider.toolsets[1] == {"skill_read", "sql_query_ro"}
    # o corpo real da skill foi entregue ao modelo como resultado de tool
    fed_back = [m.content for m in messages if m.role == MessageRole.USER]
    assert any("SKILL: postgres-neo" in c and "sql_query_ro" in c for c in fed_back)


@pytest.mark.asyncio
async def test_full_flow_reference_read():
    _, _, tool_executor, _ = _wire()
    result = await tool_executor.execute_tool_call(
        ToolCall(id="1", name="skill_read",
                 arguments={"name": "postgres-neo", "file": "references/schema.md"})
    )
    assert result.error is None
    assert "conversation_session" in str(result.result)
