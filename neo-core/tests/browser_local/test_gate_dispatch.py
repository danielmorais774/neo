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
from shared import ToolCall


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
async def test_nao_pediu_fica_local_mas_sem_janela():
    """A falha grave continua sendo tomar conta do Chrome dele sem pedir — mas
    quem evita isso agora é o modo HEADLESS, não o rebaixamento.

    Rebaixar para o servidor mandava o trabalho para a origem PIOR de propósito:
    a VPS sai por IP de datacenter na França e leva 403/CAPTCHA na web
    brasileira (job 36a81dd6, 2026-09-20: 22 passos, 51 CAPTCHAs, nada
    entregue). Headless na máquina dele tem o IP certo E não toca a tela.
    """
    ex = _executor("procura o preço do teclado")
    assert await ex._gate_browser("browser_local_task") == "browser_local_task"
    result = await ex._browser_local_task({"task": "preço do teclado", "title": "t"})
    assert result["ok"] is True
    spec = ex.db.criados[-1].spec_json
    assert spec["headless"] is True, "sem pedido explícito, nunca com janela"


@pytest.mark.asyncio
async def test_pediu_para_ver_abre_com_janela():
    """O erro simétrico: pediu para VER e o Neo ia fazer escondido."""
    ex = _executor("abre o github no meu navegador e me diz o título")
    result = await ex._browser_local_task({"task": "abre o github", "title": "t"})
    assert result["ok"] is True
    assert ex.db.criados[-1].spec_json["headless"] is False


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
    # Continua na máquina dele — é a origem certa — só que sem herdar a JANELA.
    # Que o modo vire headless está fixado em `test_nao_pediu_fica_local_mas_sem_janela`.
    assert await ex._gate_browser("browser_local_task") == "browser_local_task"


@pytest.mark.asyncio
async def test_job_de_outra_sessao_nao_herda():
    recente = SimpleNamespace(
        kind="browser_local",
        session_id=uuid4(),  # outra conversa
        created_at=datetime.utcnow() - timedelta(minutes=1),
    )
    ex = _executor("agora ordena por preço", jobs=[recente])
    assert await ex._gate_browser("browser_local_task") == "browser_local_task"


def _job_local(session_id, *, minutos: float) -> SimpleNamespace:
    return SimpleNamespace(
        kind="browser_local",
        session_id=session_id,
        created_at=datetime.utcnow() - timedelta(minutes=minutos),
    )


@pytest.mark.asyncio
async def test_continuacao_de_tarefa_local_promove_o_servidor():
    """Sessão b230a40d: o quiz rodava na aba dele e "ainda tá lá dar pra continuar"
    saiu como `browser_task` — o servidor não tem a aba nem as 15 respostas."""
    session_id = uuid4()
    ex = _executor(
        "ainda ta lá dar pra continuar", jobs=[_job_local(session_id, minutos=2)],
        session_id=session_id,
    )
    assert await ex._gate_browser("browser_task") == "browser_local_task"


@pytest.mark.asyncio
async def test_tarefa_local_velha_nao_promove():
    session_id = uuid4()
    ex = _executor(
        "continua", jobs=[_job_local(session_id, minutos=45)], session_id=session_id
    )
    assert await ex._gate_browser("browser_task") == "browser_task"


@pytest.mark.asyncio
async def test_tarefa_local_de_outra_conversa_nao_promove():
    ex = _executor("continua", jobs=[_job_local(uuid4(), minutos=1)])
    assert await ex._gate_browser("browser_task") == "browser_task"


@pytest.mark.asyncio
async def test_continuacao_sem_device_nao_promove():
    session_id = uuid4()
    ex = JobToolExecutor(
        db=_FakeDB([_job_local(session_id, minutos=1)]),
        job_manager=SimpleNamespace(poke=lambda: None),
        user_id=uuid4(),
        session_id=session_id,
        user_message="continua",
        device_id=None,
    )
    ex._enabled = True
    assert await ex._gate_browser("browser_task") == "browser_task"


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
async def test_sem_device_cai_para_o_servidor_em_vez_de_desistir():
    """Canal que não identifica a máquina (WhatsApp, neo-panel) não tem navegador
    local — mas DESISTIR era pior que o problema: o trabalho simplesmente não
    acontecia. O servidor faz a mesma coisa de uma origem que parte da web
    recusa; tentar lá e dizer a verdade se apanhar é melhor que não tentar."""
    ex = JobToolExecutor(
        db=_FakeDB(),
        job_manager=SimpleNamespace(poke=lambda: None),
        user_id=uuid4(),
        session_id=uuid4(),
        user_message="no meu navegador, abre o github",
        device_id=None,
    )
    result = await ex._browser_local_task({"task": "abre o github", "title": "t"})
    # Virou job DE SERVIDOR, não local — e o cérebro é avisado do porquê.
    assert result["ok"] is True
    assert ex.db.criados[-1].kind == "browser"
    assert "SERVIDOR" in result["note"]
    assert "desktop" in result["note"]


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


# ── pedido recente vale para a mensagem seguinte (session-30b1e484) ────────
#
# "vamos fazer pelo meu navegador local - ou seja - caminho A" e, um minuto
# depois, "nao foi, acessa o meu portifolio ai". O cérebro chamou
# `browser_local_task`, a segunda frase não tinha âncora, não havia job local
# anterior para herdar — e o portão rebaixou para o servidor. Duas vezes.


def _com_historico(message, falas, *, minutos=1):
    ex = _executor(message)
    msgs = [
        SimpleNamespace(
            role="user", content=fala, created_at=datetime.utcnow() - timedelta(minutes=minutos)
        )
        for fala in falas
    ]

    async def _fetch(session_id, limit=50, roles=None, **kw):
        return msgs[-limit:]

    ex.db.fetch_session_messages = _fetch
    return ex


@pytest.mark.asyncio
async def test_pedido_de_navegador_local_na_mensagem_anterior_mantem_o_local():
    ex = _com_historico(
        "nao foi, acessa o meu portifolio ai",
        [
            "vamos fazer pelo meu navegador local - ou seja - caminho A",
            "nao foi, acessa o meu portifolio ai",
        ],
    )
    assert await ex._gate_browser("browser_local_task") == "browser_local_task"


@pytest.mark.asyncio
async def test_passo_de_continuacao_le_a_frase_do_usuario_no_banco():
    """No ciclo, `user_message` é a ordem que o Neo sintetizou, não a do usuário."""
    ex = _com_historico(
        "avertir o usuário quando a tarefa do browser terminar",
        ["entao acessa pelo meu navegador local"],
    )
    assert await ex._gate_browser("browser_local_task") == "browser_local_task"


@pytest.mark.asyncio
async def test_pedido_velho_da_conversa_segura_a_janela():
    """Pedido de 30 min atrás, NA MESMA CONVERSA, ainda vale como "quero ver".

    Antes caducava em 10 minutos (sessão 7bc7caa1: ele respondeu uma pergunta
    do Neo 17 minutos depois e a tarefa foi para o headless). A escolha é da
    conversa inteira, até ele dizer "sem janela".
    """
    ex = _com_historico("acessa o site", ["no meu navegador local"], minutos=30)
    assert await ex._gate_browser("browser_local_task") == "browser_local_task"
    result = await ex._browser_local_task({"task": "acessa o site", "title": "t"})
    assert result["ok"] is True
    assert ex.db.criados[-1].spec_json["headless"] is False
    assert result["janela"] is True


@pytest.mark.asyncio
async def test_historico_da_conversa_promove_o_servidor():
    """Pedido do Daniel (13/11): conversa que pediu o navegador dele usa o
    navegador dele em TODO pedido de navegador — também quando o cérebro
    escolheu o servidor. Antes o histórico só mantinha, nunca promovia."""
    ex = _com_historico("B", ["entao acessa pelo meu navegador local"])
    assert await ex._gate_browser("browser_task") == "browser_local_task"


@pytest.mark.asyncio
async def test_historico_nao_promove_sem_maquina_no_canal():
    """Sem device no canal (painel/WhatsApp) não há navegador dele para onde ir."""
    ex = _com_historico("B", ["entao acessa pelo meu navegador local"])
    ex.device_id = None
    assert await ex._gate_browser("browser_task") == "browser_task"


@pytest.mark.asyncio
async def test_sem_janela_depois_desfaz_a_promocao():
    """A saída é dele: "faz sem janela" depois do pedido tira a janela da conversa."""
    ex = _com_historico("B", ["entao acessa pelo meu navegador local"], minutos=20)
    msgs_antigas = await ex.db.fetch_session_messages(ex.session_id)
    depois = SimpleNamespace(
        role="user", content="daqui pra frente faz sem janela",
        created_at=datetime.utcnow() - timedelta(minutes=5),
    )

    async def _fetch(session_id, limit=50, roles=None, **kw):
        return [*msgs_antigas, depois]

    ex.db.fetch_session_messages = _fetch
    assert await ex._gate_browser("browser_task") == "browser_task"


@pytest.mark.asyncio
async def test_rebaixado_avisa_o_cerebro_que_nao_foi_na_tela_dele(monkeypatch):
    """O resultado voltava igual nos dois casos e o Neo disse "abri no seu navegador"."""
    from shared.config import get_settings

    monkeypatch.setattr(get_settings(), "browser_agent_enabled", True, raising=False)
    # Só há rebaixamento de verdade quando NÃO existe máquina do usuário neste
    # canal — com device, o local headless vence (ver o teste acima).
    ex = _executor("acessa o meu portifolio")
    ex.device_id = None
    tr = await ex.execute_tool_call(
        ToolCall(id="1", name="browser_local_task", arguments={"task": "abre https://x.dev", "title": "t"})
    )
    assert tr.name == "browser_task"
    assert tr.result["onde_rodou"] == "servidor"
    assert "NÃO para a tela do usuário" in tr.result["aviso"]


@pytest.mark.asyncio
async def test_sem_troca_o_resultado_fica_como_era(monkeypatch):
    from shared.config import get_settings

    monkeypatch.setattr(get_settings(), "browser_agent_enabled", True, raising=False)
    ex = _executor("procura o preço do teclado")
    tr = await ex.execute_tool_call(
        ToolCall(id="1", name="browser_task", arguments={"task": "preço do teclado", "title": "t"})
    )
    assert "aviso" not in tr.result and "onde_rodou" not in tr.result


# ── mesma tarefa de browser não vira dois jobs (session-30b1e484) ──────────


def _browser_vivo(session_id, task, *, status="running", minutos=2):
    return SimpleNamespace(
        id=uuid4(),
        kind="browser",
        status=status,
        title="Análise de Portfólio Antigo",
        session_id=session_id,
        spec_json={"task": task, "session": "user-default"},
        created_at=datetime.utcnow() - timedelta(minutes=minutos),
    )


@pytest.fixture
def _browser_do_servidor_ligado(monkeypatch):
    from shared.config import get_settings

    monkeypatch.setattr(get_settings(), "browser_agent_enabled", True, raising=False)


@pytest.mark.asyncio
async def test_mesmo_site_na_mesma_conversa_reusa_o_job(_browser_do_servidor_ligado):
    sid = uuid4()
    vivo = _browser_vivo(sid, "Acesse https://danielmorais.vercel.app/ e analise o conteúdo")
    ex = _executor("acessa o site e faz uma analise", jobs=[vivo], session_id=sid)

    result = await ex._browser_task(
        {"task": "Acesse https://danielmorais.vercel.app e verifique o conteúdo atual. Tire screenshot."}
    )

    assert result["reused"] is True
    assert result["job_id"] == str(vivo.id)
    assert ex.db.criados == []


@pytest.mark.asyncio
async def test_site_diferente_cria_job_novo(_browser_do_servidor_ligado):
    sid = uuid4()
    vivo = _browser_vivo(sid, "Acesse https://danielmorais.vercel.app/")
    ex = _executor("agora olha o github", jobs=[vivo], session_id=sid)
    result = await ex._browser_task({"task": "Acesse https://github.com/danielmorais"})
    assert result.get("reused") is None
    assert len(ex.db.criados) == 1


@pytest.mark.asyncio
async def test_job_terminado_ou_de_outra_conversa_nao_conta(_browser_do_servidor_ligado):
    sid = uuid4()
    tarefa = "Acesse https://danielmorais.vercel.app/"
    ex = _executor(
        "analisa de novo",
        jobs=[
            _browser_vivo(sid, tarefa, status="timeout"),
            _browser_vivo(uuid4(), tarefa),
            _browser_vivo(sid, tarefa, minutos=45),
        ],
        session_id=sid,
    )
    result = await ex._browser_task({"task": tarefa})
    assert result.get("reused") is None
    assert len(ex.db.criados) == 1


@pytest.mark.asyncio
async def test_sem_endereco_so_a_tarefa_identica_conta(_browser_do_servidor_ligado):
    sid = uuid4()
    ex = _executor(
        "pesquisa",
        jobs=[_browser_vivo(sid, "Pesquise passagens para São Paulo")],
        session_id=sid,
    )
    diferente = await ex._browser_task({"task": "Pesquise hotéis em São Paulo"})
    igual = await ex._browser_task({"task": "pesquise  passagens para são paulo"})
    assert diferente.get("reused") is None
    assert igual["reused"] is True
