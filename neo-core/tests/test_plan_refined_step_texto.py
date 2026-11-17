"""O refino do plano devolveu ``tool_arguments`` como TEXTO e o turno morreu.

Sessão e9947724 (16/11/2026), no retry do passo que falhou (``whisper`` não
instalado): ``next_step.tool_arguments.get("command", …)`` em
``process_tool_result`` levantou ``AttributeError: 'str' object has no attribute
'get'`` dentro do gRPC. Nenhuma resposta chegou ao desktop.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.planning.plan_manager import ExecutionPlan, PlanManager, PlanStep

_norm = PlanManager._normalizar_refined_step


def test_argumentos_em_json_texto_viram_objeto():
    r = _norm({"tool_name": "run_command", "tool_arguments": '{"command": "ls -la"}'})
    assert r == {"tool_name": "run_command", "tool_arguments": {"command": "ls -la"}}


def test_argumentos_em_texto_solto_saem_e_nunca_viram_comando():
    r = _norm({"tool_name": "run_command", "tool_arguments": "pip install openai-whisper"})
    assert r == {"tool_name": "run_command"}


@pytest.mark.parametrize("args", [None, ["ls"], 42])
def test_argumentos_que_nao_sao_objeto_saem(args):
    assert "tool_arguments" not in _norm({"tool_name": "run_command", "tool_arguments": args})


def test_refined_step_inteiro_em_texto():
    assert _norm('{"tool_name": "run_command", "tool_arguments": {"command": "pwd"}}') == {
        "tool_name": "run_command", "tool_arguments": {"command": "pwd"},
    }
    assert _norm("roda o pip") is None
    assert _norm(None) is None
    assert _norm({"tool_name": "x"}) == {"tool_name": "x"}


class _Provider:
    def __init__(self, resposta: dict):
        self.resposta = resposta

    async def complete(self, messages, **kw):
        return SimpleNamespace(get_content=lambda: json.dumps(self.resposta))


def _pm(resposta: dict) -> PlanManager:
    provider = _Provider(resposta)
    pm = PlanManager.__new__(PlanManager)
    pm.llm = SimpleNamespace(get_provider=lambda *a, **k: provider)
    pm.settings = None
    pm._plans = {}
    pm._subplan_history = {}
    return pm


def _plano() -> ExecutionPlan:
    whisper = PlanStep(index=0, description="Transcrever com whisper", tool_name="run_command",
                       tool_arguments={"command": "whisper x.wav"})
    whisper.status = "failed"
    ler = PlanStep(index=1, description="Ler o .srt", tool_name="run_command",
                   tool_arguments={"command": "cat x.srt"}, depends_on_previous=True)
    return ExecutionPlan(goal="transcrever", steps=[whisper, ler])


@pytest.mark.asyncio
async def test_advance_and_refine_entrega_objeto_mesmo_com_texto_do_modelo():
    pm = _pm({
        "validation": {"success": False}, "decision": "retry", "reason": "whisper não instalado",
        "refined_step": {"tool_name": "run_command", "tool_arguments": "pip install openai-whisper"},
    })
    r = await pm.advance_and_refine(plan=_plano(), step_index=0, raw_output="ERRO: whisper: command not found")
    refinado = r["refined_step"]
    assert refinado == {"tool_name": "run_command"}
    # o que process_tool_result faz com ele, nos três lugares
    passo = _plano().steps[1]
    passo.tool_arguments = refinado.get("tool_arguments", passo.tool_arguments)
    assert passo.tool_arguments.get("command") == "cat x.srt"


@pytest.mark.asyncio
async def test_refine_step_com_texto_nao_quebra():
    """Antes, o ``except`` engolia o AttributeError mas deixava o TEXTO no passo."""
    pm = _pm({"tool_name": "run_command", "tool_arguments": "cat x.srt"})
    plano = _plano()
    passo = plano.steps[1]
    pronto = await pm.refine_step(plan=plano, step=passo)
    assert pronto.tool_arguments == {"command": "cat x.srt"}
    assert isinstance(pronto.tool_arguments, dict)
