"""O portão dentro do JobToolExecutor: rebaixar e promover de verdade.

O ``gate.py`` decide se a FRASE pediu o navegador local; aqui o que está sob
teste é o que o executor FAZ com essa decisão — inclusive a herança de
continuação, que é lida do banco de jobs (não de cache de processo, porque o
core reinicia no meio da conversa).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.jobs.executor import JobToolExecutor


class _FakeDB:
    def __init__(self, jobs=None) -> None:
        self.jobs = jobs or []
        self.criados = []

    async def list_agent_jobs(self, user_id, *, statuses=None, limit=20):
        return self.jobs

    async def create_agent_job(self, job):
        # O banco atribui o id no INSERT; o dublê imita para quem lê job.id.
        if getattr(job, "id", None) is None:
            job.id = uuid4()
        self.criados.append(job)
        return job


def _executor(message: str, *, jobs=None, session_id=None, enabled=True):
    ex = JobToolExecutor(
        db=_FakeDB(jobs),
        job_manager=SimpleNamespace(poke=lambda: None),
        user_id=uuid4(),
        session_id=session_id or uuid4(),
        user_message=message,
        device_id="device-1",
    )
    ex._enabled = enabled
    return ex


@pytest.fixture(autouse=True)
def _liga_o_local(monkeypatch):
    """A flag fica ON nestes testes — o OFF tem teste próprio."""
    from shared.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "browser_local_enabled", True, raising=False)
    get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
    monkeypatch.setattr("shared.config.get_settings", lambda: settings)
    yield


@pytest.mark.asyncio
async def test_pediu_local_e_escolheu_local_segue():
    ex = _executor("no meu navegador, abre o github")
    assert await ex._gate_browser("browser_local_task") == "browser_local_task"


@pytest.mark.asyncio
async def test_nao_pediu_e_escolheu_local_rebaixa():
    """A falha grave: tomar conta do Chrome do usuário sem ele pedir."""
    ex = _executor("procura o preço do teclado")
    assert await ex._gate_browser("browser_local_task") == "browser_task"


@pytest.mark.asyncio
async def test_pediu_local_e_escolheu_servidor_promove():
    """O erro simétrico: pediu para VER e o Neo ia fazer escondido."""
    ex = _executor("abre o github no meu navegador e me diz o título")
    assert await ex._gate_browser("browser_task") == "browser_local_task"


@pytest.mark.asyncio
async def test_nao_pediu_e_escolheu_servidor_segue():
    ex = _executor("procura passagens para São Paulo")
    assert await ex._gate_browser("browser_task") == "browser_task"


@pytest.mark.asyncio
async def test_continuacao_herda_o_local_da_sessao():
    """'agora ordena por preço' continua na aba que o usuário está olhando."""
    session_id = uuid4()
    recente = SimpleNamespace(
        kind="browser_local",
        session_id=session_id,
        created_at=datetime.utcnow() - timedelta(minutes=2),
    )
    ex = _executor("agora ordena por preço", jobs=[recente], session_id=session_id)
    assert await ex._gate_browser("browser_local_task") == "browser_local_task"


@pytest.mark.asyncio
async def test_continuacao_velha_nao_herda():
    session_id = uuid4()
    antigo = SimpleNamespace(
        kind="browser_local",
        session_id=session_id,
        created_at=datetime.utcnow() - timedelta(minutes=45),
    )
    ex = _executor("agora ordena por preço", jobs=[antigo], session_id=session_id)
    assert await ex._gate_browser("browser_local_task") == "browser_task"


@pytest.mark.asyncio
async def test_job_de_outra_sessao_nao_herda():
    recente = SimpleNamespace(
        kind="browser_local",
        session_id=uuid4(),  # outra conversa
        created_at=datetime.utcnow() - timedelta(minutes=1),
    )
    ex = _executor("agora ordena por preço", jobs=[recente])
    assert await ex._gate_browser("browser_local_task") == "browser_task"


@pytest.mark.asyncio
async def test_tools_que_nao_sao_de_browser_passam_intactas():
    ex = _executor("no meu navegador, faz tudo")
    assert await ex._gate_browser("deep_research") == "deep_research"
    assert await ex._gate_browser("neo_bash_background") == "neo_bash_background"


@pytest.mark.asyncio
async def test_flag_desligada_rebaixa_tudo(monkeypatch):
    from shared.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "browser_local_enabled", False, raising=False)
    ex = _executor("no meu navegador, abre o github")
    assert await ex._gate_browser("browser_local_task") == "browser_task"


@pytest.mark.asyncio
async def test_sem_device_a_tool_recusa_em_vez_de_enfileirar():
    """Canal que não identifica a máquina (WhatsApp, painel) não pode virar job."""
    ex = JobToolExecutor(
        db=_FakeDB(),
        job_manager=SimpleNamespace(poke=lambda: None),
        user_id=uuid4(),
        session_id=uuid4(),
        user_message="no meu navegador, abre o github",
        device_id=None,
    )
    result = await ex._browser_local_task({"task": "abre o github", "title": "t"})
    assert result["ok"] is False
    assert "máquina" in result["error"]


# ── uma tarefa por navegador (job 698446b8: "Debugger unattached") ────────
#
# Existe UMA aba reivindicada por device. Em 2026-09-01 o cérebro disparou
# `browser_local_task` duas vezes — viu `job_status: running`, viu
# `job_logs: (sem logs)` e achou que a primeira não tinha pegado. Quando o job 1
# terminou, soltou a aba (`release` → `detach`) e derrubou o debugger do job 2
# no meio do passo. O job 1 estava indo bem e respondeu certo (24,64 GB).


def _job_vivo(device="device-1", status="running", kind="browser_local"):
    return SimpleNamespace(
        id=uuid4(),
        kind=kind,
        status=status,
        title="Ver tamanho da pasta palworld",
        session_id=uuid4(),
        spec_json={"device_id": device},
        created_at=datetime.utcnow(),
    )


@pytest.mark.asyncio
async def test_segunda_tarefa_no_mesmo_navegador_reusa_a_primeira():
    vivo = _job_vivo()
    ex = _executor("qual o tamanho do arquivo", jobs=[vivo])
    result = await ex._browser_local_task({"task": "ver o tamanho", "title": "t"})
    assert result["ok"] is True
    assert result["reused"] is True
    assert result["job_id"] == str(vivo.id), "tem que devolver o job que já existe"
    assert "derruba a primeira" in result["note"]


@pytest.mark.asyncio
async def test_job_vivo_em_OUTRO_device_nao_atrapalha():
    """A aba é por máquina: o navegador do outro aparelho é outro navegador."""
    ex = _executor("abre o drive", jobs=[_job_vivo(device="device-2")])
    result = await ex._browser_local_task({"task": "abre o drive", "title": "t"})
    assert result.get("reused") is None, "devia ter criado um job novo"


@pytest.mark.asyncio
async def test_job_terminado_nao_bloqueia_o_proximo():
    """A trava é só enquanto a aba está OCUPADA — `list_agent_jobs` filtra por
    status, mas quem chama tem que pedir o filtro certo."""
    pedidos = {}

    class _DB(_FakeDB):
        async def list_agent_jobs(self, user_id, *, statuses=None, limit=20):
            pedidos["statuses"] = statuses
            return []

    ex = _executor("abre o drive")
    ex.db = _DB()
    result = await ex._browser_local_task({"task": "abre o drive", "title": "t"})
    assert pedidos["statuses"] == ["queued", "running"]
    assert result.get("reused") is None


@pytest.mark.asyncio
async def test_banco_fora_do_ar_nao_bloqueia_a_tarefa():
    """Consulta de diagnóstico não pode impedir o trabalho de acontecer."""

    class _DB(_FakeDB):
        async def list_agent_jobs(self, *a, **kw):
            raise RuntimeError("banco fora")

    ex = _executor("abre o drive")
    ex.db = _DB()
    assert await ex._browser_local_em_curso() is None


# ── retomada: continue_job (F4) ──────────────────────────────────────────


def _job_suspenso(pending="Excluir definitivamente", tab=77):
    return SimpleNamespace(
        id=uuid4(),
        kind="browser_local",
        status="failed",
        title="Deletar Palworld-SteamRIP.com.rar do Drive",
        session_id=uuid4(),
        spec_json={
            "task": "No Drive, apague o arquivo Palworld-SteamRIP.com.rar",
            "device_id": "device-1",
            "context": "pasta palworld aberta",
            "max_steps": 25,
        },
        extra_metadata={
            "browser_session": {
                "device_id": "device-1",
                "tab_ref": tab,
                "url": "https://drive.google.com/drive/trash",
                "pending": pending,
                "status": "needs_confirmation",
            }
        },
        created_at=datetime.utcnow(),
    )


def _com_retomada(anterior, jobs=None):
    ex = _executor("pode apagar sim", jobs=jobs)

    async def _owned(_job_id):
        return anterior

    ex._get_owned = _owned  # type: ignore[assignment]
    return ex


@pytest.mark.asyncio
async def test_retomada_herda_tarefa_aba_e_o_clique_autorizado():
    anterior = _job_suspenso()
    ex = _com_retomada(anterior)
    result = await ex._browser_local_task({"continue_job": str(anterior.id)})

    assert result["ok"] is True
    assert result["resumed_from"] == str(anterior.id)
    spec = ex.db.criados[0].spec_json
    assert spec["task"] == anterior.spec_json["task"], "não pode reescrever a tarefa"
    assert spec["tab_ref"] == 77, "sem a aba, recomeça em about:blank"
    assert spec["resume_pending"] == "Excluir definitivamente"
    assert spec["confirm_sensitive_ok"] is True


@pytest.mark.asyncio
async def test_retomada_dispensa_task_e_title():
    """O cérebro manda SÓ o continue_job — a validação de task não pode barrar."""
    anterior = _job_suspenso()
    result = await _com_retomada(anterior)._browser_local_task(
        {"continue_job": str(anterior.id)}
    )
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_job_que_nao_parou_para_perguntar_nao_e_retomavel():
    """Sem clique pendente não há permissão a herdar — e herdar seria grave."""
    anterior = _job_suspenso(pending="")
    result = await _com_retomada(anterior)._browser_local_task(
        {"continue_job": str(anterior.id)}
    )
    assert result["ok"] is False
    assert "não parou esperando confirmação" in result["error"]


@pytest.mark.asyncio
async def test_retomada_de_job_inexistente_avisa_em_vez_de_criar():
    ex = _executor("pode apagar")

    async def _owned(_job_id):
        return None

    ex._get_owned = _owned  # type: ignore[assignment]
    result = await ex._browser_local_task({"continue_job": str(uuid4())})
    assert result["ok"] is False
    assert ex.db.criados == []


@pytest.mark.asyncio
async def test_retomada_respeita_a_trava_de_uma_tarefa_por_navegador():
    anterior = _job_suspenso()
    ex = _com_retomada(anterior, jobs=[_job_vivo()])
    result = await ex._browser_local_task({"continue_job": str(anterior.id)})
    assert result.get("reused") is True
    assert ex.db.criados == [], "não pode criar job novo com a aba ocupada"


@pytest.mark.asyncio
async def test_tarefa_normal_nao_vira_retomada():
    """Regressão: sem continue_job, tudo segue exatamente como antes."""
    ex = _executor("abre o drive")
    result = await ex._browser_local_task({"task": "abre o drive", "title": "t"})
    assert result["ok"] is True
    assert "resumed_from" not in result
    assert "resume_pending" not in ex.db.criados[0].spec_json
