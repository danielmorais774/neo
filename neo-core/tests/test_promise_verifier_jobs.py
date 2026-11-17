"""Verificador de promessa x job aberto por tool (session-1fcaa440).

"Vou avisar quando sair" logo depois de um `neo_bash_background` com `then` foi
lido como promessa pendente: o verificador sintetizou "monitorar o OCR" e abriu um
ciclo paralelo de 50 chamadas de LLM que refez o trabalho da continuação.
"""

from modules.desktop.ai_service import _open_jobs, _summarize_tool_call
from shared import ToolResult


def _trace_job(then: str | None):
    args = {"command": "python3 /skills/ocr-video/scripts/ocr_video.py v.mov", "title": "OCR"}
    if then:
        args["then"] = then
    tr = ToolResult(tool_call_id="1", name="neo_bash_background", result={"ok": True, "job_id": "j1", "title": "OCR"}, execution_time=0.0)
    return _summarize_tool_call("neo_bash_background", args, tr)


def test_job_with_then_counts_as_dispatched_and_skips_the_verifier():
    linhas, tem_continuacao = _open_jobs([_trace_job("entregue o txt")])
    assert tem_continuacao is True
    assert linhas and "OCR" in linhas[0] and "continuação automática" in linhas[0]


def test_job_without_then_is_still_listed_as_dispatched():
    linhas, tem_continuacao = _open_jobs([_trace_job(None)])
    assert tem_continuacao is False
    assert linhas == ["job em segundo plano: OCR (job j1)"]


def test_turn_without_jobs_changes_nothing():
    assert _open_jobs([{"tool": "neo_bash", "ok": True}]) == ([], False)
    assert _open_jobs(None) == ([], False)
