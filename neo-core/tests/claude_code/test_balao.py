"""O botão do processo no balão: que tarefa do Claude cada resposta abriu."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

from api.routes.desktop import _serialize_chat_message
from core.claude_code.balao import tarefas_da_mensagem, tarefas_do_balao
from grpc_server import NeoDesktopServicer
from modules.desktop.ai_service import _summarize_tool_call


def test_tarefa_e_continuacao_sem_repetir_e_sem_outras_tools():
    trace = [
        {"tool": "neo_bash_background", "async_job": True, "job_id": "bash-1"},
        {"tool": "claude_task", "ok": True, "async_job": True, "job_id": "c-1"},
        {"tool": "claude_reply", "ok": True, "job_id": "c-2"},
        {"tool": "claude_reply", "ok": True, "job_id": "c-2"},
        {"tool": "claude_task", "ok": True, "summary": "recusado"},  # sem job: não abriu nada
        None,
    ]
    assert tarefas_do_balao(trace) == [
        {"id": "c-1", "kind": "claude_code"},
        {"id": "c-2", "kind": "claude_code"},
    ]
    assert tarefas_do_balao(None) == []


def test_claude_reply_guarda_o_id_da_continuacao_no_trace():
    resultado = {"ok": True, "job_id": "c-novo", "liberadas": [], "note": "O Claude continua."}
    entrada = _summarize_tool_call(
        "claude_reply", {"job_id": "c-velho", "mensagem": "pode"}, SimpleNamespace(error=None, result=resultado)
    )
    assert entrada["job_id"] == "c-novo"
    assert "summary" in entrada  # o prompt do próximo turno continua vendo o mesmo resumo
    recusa = _summarize_tool_call(
        "claude_reply", {"job_id": "x"}, SimpleNamespace(error=None, result={"ok": False, "error": "já terminou"})
    )
    assert "job_id" not in recusa


def test_historico_reaberto_traz_o_mesmo_botao():
    """Resposta gravada antes do campo existir também ganha o botão: vem do tool_trace."""
    msg = SimpleNamespace(
        id=uuid4(),
        role="assistant",
        content="Mandei pro Claude.",
        created_at=datetime(2026, 11, 7, 22, 5, 58),
        mission_id=None,
        extra_metadata={"tool_trace": [{"tool": "claude_task", "async_job": True, "job_id": "0a33cb31"}]},
    )
    assert _serialize_chat_message(msg)["jobs"] == [{"id": "0a33cb31", "kind": "claude_code"}]
    msg.extra_metadata = {}
    assert "jobs" not in _serialize_chat_message(msg)


def test_grpc_leva_as_tarefas_na_resposta():
    refs = NeoDesktopServicer._jobs_pb([{"id": "c-1", "kind": "claude_code"}, {"kind": "sem id"}])
    assert [(r.id, r.kind) for r in refs] == [("c-1", "claude_code")]
    assert NeoDesktopServicer._jobs_pb(None) == []


def test_entrega_do_resultado_aponta_para_a_tarefa():
    entrega = {"kind": "claude_code", "job_id": "0a33cb31", "source": "agent_job"}
    assert tarefas_da_mensagem(entrega) == [{"id": "0a33cb31", "kind": "claude_code"}]
    # entrega de outro tipo de tarefa (navegador) não vira botão do Claude
    assert tarefas_da_mensagem({"kind": "browser_local", "job_id": "b", "source": "agent_job"}) == []
    assert tarefas_da_mensagem(None) == []


def test_tarefa_do_claude_diz_onde_retomar_no_terminal():
    from api.routes.desktop import _serialize_job

    job = SimpleNamespace(
        id=uuid4(), kind="claude_code", title="Claude: x", status="completed", notify_policy="chat",
        session_id=None, created_at=None, started_at=None, finished_at=None, heartbeat_at=None,
        exit_code=None, result_summary="ok", progress_note="", log_path=None,
        spec_json={"objetivo": "x", "claude": {"session_id": "s1", "cwd": "/Users/d/p", "sessao_iniciada": True}},
        extra_metadata={},
    )
    assert _serialize_job(job)["claude"] == {"session_id": "s1", "cwd": "/Users/d/p", "rodando": False}
    job.status = "running"
    assert _serialize_job(job)["claude"]["rodando"] is True
    # antes da 1ª rodada não há sessão para retomar
    job.spec_json = {"objetivo": "x", "claude": {"session_id": "s1", "cwd": "/Users/d/p"}}
    assert "claude" not in _serialize_job(job)
    job.kind = "browser_local"
    assert "claude" not in _serialize_job(job)
