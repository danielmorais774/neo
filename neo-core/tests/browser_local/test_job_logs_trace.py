"""`job_logs` num job de navegador local lia o lugar errado.

O diário do navegador mora no DISCO do neo-core (`logs/browser_local/<job>/
trace.md`), não no sandbox do workbench. O `_logs` sempre lia
`.jobs/<id>.log` no sandbox — que nunca existiu para esse tipo de job — e
devolvia "(sem logs)" com a tarefa correndo bem.

Não é um detalhe de diagnóstico: foi essa resposta vazia que convenceu o
cérebro de que a tarefa não tinha pegado, levando-o a disparar uma SEGUNDA
`browser_local_task`. As duas brigaram pela única aba do device e a primeira,
ao terminar, derrubou o debugger da outra ("Debugger unattached", job
698446b8 — enquanto a primeira respondia certo: 24,64 GB).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.jobs.executor import JobToolExecutor


class _SandboxVazio:
    """O sandbox nunca tem o log de um job de navegador — só o de bash."""

    async def read_file(self, path, max_bytes=None):
        return SimpleNamespace(ok=False, stdout="", truncated=False)

    # O log de job do sandbox é lido pelo FIM (`read_tail`).
    async def read_tail(self, path, max_bytes=None):
        return SimpleNamespace(ok=False, stdout="", truncated=False)


def _executor(job):
    ex = JobToolExecutor(
        db=SimpleNamespace(),
        job_manager=SimpleNamespace(poke=lambda: None, sandbox=_SandboxVazio()),
        user_id=uuid4(),
        session_id=uuid4(),
        user_message="como está indo?",
        device_id="device-1",
    )

    async def _owned(_job_id):
        return job

    ex._get_owned = _owned  # type: ignore[assignment]
    return ex


def _job(log_path, status="running"):
    return SimpleNamespace(
        id=uuid4(), status=status, log_path=str(log_path), result_summary=""
    )


@pytest.mark.asyncio
async def test_le_o_trace_do_navegador_do_disco(tmp_path):
    diario = tmp_path / "job-1"
    diario.mkdir()
    (diario / "trace.md").write_text(
        "# Tarefa no navegador local\n\n## Passo 2/25\n- escolheu: dblclick\n",
        encoding="utf-8",
    )
    ex = _executor(_job(diario))
    out = await ex._logs({"job_id": "x"})
    assert out["ok"] is True
    assert "Passo 2/25" in out["logs"], "o progresso real tem que chegar ao cérebro"
    assert "(sem logs)" not in out["logs"]


@pytest.mark.asyncio
async def test_trace_grande_vem_pelo_FIM(tmp_path):
    """Como está indo AGORA são os últimos passos, não o cabeçalho."""
    diario = tmp_path / "job-2"
    diario.mkdir()
    corpo = "\n".join(f"## Passo {i}/25 · linha de enchimento" for i in range(1, 900))
    (diario / "trace.md").write_text(corpo, encoding="utf-8")
    ex = _executor(_job(diario))
    out = await ex._logs({"job_id": "x", "tail": 500})
    assert out["truncated"] is True
    assert "Passo 899" in out["logs"]
    assert "Passo 1/25" not in out["logs"]
    # Corte cego partiria uma linha ao meio; a primeira parcial é descartada.
    assert not out["logs"].startswith("enchimento")


@pytest.mark.asyncio
async def test_diario_recem_criado_nao_vira_sem_logs(tmp_path):
    """Vazio de verdade tem que dizer 'acabou de começar', não 'sem logs' — a
    diferença entre 'espera' e 'não pegou, dispara de novo'."""
    diario = tmp_path / "job-3"
    diario.mkdir()
    (diario / "trace.md").write_text("", encoding="utf-8")
    out = await _executor(_job(diario))._logs({"job_id": "x"})
    assert "acabou de começar" in out["logs"]


@pytest.mark.asyncio
async def test_job_de_bash_continua_indo_no_sandbox(tmp_path):
    """Caminho que não existe no disco cai no sandbox, como antes."""
    ex = _executor(_job(tmp_path / "nao-existe"))
    out = await ex._logs({"job_id": "x"})
    assert out["ok"] is True
    assert out["logs"] == "(sem logs)"
