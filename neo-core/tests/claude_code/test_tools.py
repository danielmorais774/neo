"""As tools do Claude: portão, continuação pela mesma sessão e a entrega no chat."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from core.authority import NEGAR, PERMITIR, PRINCIPAL_AGENDA, PRINCIPAL_EXTERNO, TurnAuthority, evaluate
from core.claude_code.orquestrador import Desfecho
from core.claude_code.stream import Cota, Recusa
from core.jobs.executor import JobToolExecutor
from core.jobs.runner_claude import _terminal
from db.models.agent_job import AgentJob


# ── portão ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool", ["claude_task", "claude_reply"])
def test_turno_limpo_do_daniel_passa(tool):
    assert evaluate(tool, TurnAuthority()).efeito == PERMITIR


@pytest.mark.parametrize("tool", ["claude_task", "claude_reply"])
@pytest.mark.parametrize("fonte", ["web_fetch", "email_message", "colagem"])
def test_turno_contaminado_nega_ate_por_leitura_de_web(tool, fonte):
    autoridade = TurnAuthority()
    autoridade.taint(fonte)
    decisao = evaluate(tool, autoridade)
    assert decisao.efeito == NEGAR


@pytest.mark.parametrize("principal", [PRINCIPAL_AGENDA, PRINCIPAL_EXTERNO])
def test_agenda_e_pedido_de_fora_negam(principal):
    assert evaluate("claude_task", TurnAuthority(principal=principal)).efeito == NEGAR


# ── claude_reply ─────────────────────────────────────────────────────────

class _DB:
    def __init__(self, *jobs: AgentJob) -> None:
        self.jobs = {j.id: j for j in jobs}
        self.criados: list[AgentJob] = []

    async def get_agent_job(self, job_id):
        return self.jobs.get(job_id)

    async def create_agent_job(self, job):
        self.jobs[job.id] = job
        self.criados.append(job)
        return job

    async def update_agent_job(self, job_id, **campos):
        job = self.jobs[job_id]
        for k, v in campos.items():
            setattr(job, k, v)
        return job


class _JM:
    _claude_runner = object()

    def poke(self):
        pass


@pytest.fixture
def ligado(monkeypatch):
    from shared.config import get_settings

    monkeypatch.setattr(get_settings(), "claude_code_enabled", True)


def _parado(user_id, *, status="waiting") -> AgentJob:
    return AgentJob(
        user_id=user_id,
        kind="claude_code",
        title="Claude: consertar o teste",
        status=status,
        spec_json={"objetivo": "consertar o teste", "pasta": "~/proj", "corpo": "local",
                   "device_id": "mac-1", "then": "resuma", "claude": {"session_id": "velho"}},
        extra_metadata={"claude": {
            "session_id": "sess-1",
            "cwd": "/Users/d/proj",
            "tarefa_id": "t-1",
            "recusas": [
                {"ferramenta": "Bash", "entrada": {"command": "npm install"}},
                {"ferramenta": "Write", "entrada": {"file_path": "/etc/hosts"}},
            ],
        }},
    )


@pytest.mark.asyncio
async def test_resposta_com_liberacao_continua_pela_mesma_sessao(ligado):
    user = uuid.uuid4()
    parado = _parado(user)
    db = _DB(parado)
    ex = JobToolExecutor(db=db, job_manager=_JM(), user_id=user, session_id=None)
    r = await ex._claude_reply({"job_id": str(parado.id), "mensagem": "pode instalar", "liberar": True})
    assert r["ok"] and r["liberadas"] == ["Bash(npm install)"]  # o Write fora da pasta não se libera
    novo = db.criados[0]
    assert novo.kind == "claude_code" and novo.status == "queued"
    assert novo.spec_json["claude"] == {
        "session_id": "sess-1", "sessao_iniciada": True, "cwd": "/Users/d/proj", "tarefa_id": "t-1",
    }
    assert "pode instalar" in novo.spec_json["instrucao_inicial"]
    assert novo.spec_json["then"] == "resuma"
    assert parado.status == "continued"


@pytest.mark.asyncio
async def test_resposta_sem_liberar_nao_libera_nada(ligado):
    user = uuid.uuid4()
    parado = _parado(user)
    db = _DB(parado)
    ex = JobToolExecutor(db=db, job_manager=_JM(), user_id=user, session_id=None)
    r = await ex._claude_reply({"job_id": str(parado.id), "mensagem": "não instala, usa o que tem"})
    assert r["liberadas"] == []


@pytest.mark.asyncio
async def test_mensagem_com_a_tarefa_rodando_entra_na_caixa(ligado):
    user = uuid.uuid4()
    rodando = _parado(user, status="running")
    db = _DB(rodando)
    ex = JobToolExecutor(db=db, job_manager=_JM(), user_id=user, session_id=None)
    r = await ex._claude_reply({"job_id": str(rodando.id), "mensagem": "para", "interromper": True})
    assert r["ok"] and db.criados == []
    caixa = rodando.extra_metadata["claude_mensagens"]
    assert caixa[0]["texto"] == "para" and caixa[0]["interromper"] is True
    # o resto do extra_metadata continua lá
    assert rodando.extra_metadata["claude"]["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_tarefa_de_outro_usuario_nao_se_responde(ligado):
    parado = _parado(uuid.uuid4())
    ex = JobToolExecutor(db=_DB(parado), job_manager=_JM(), user_id=uuid.uuid4(), session_id=None)
    r = await ex._claude_reply({"job_id": str(parado.id), "mensagem": "oi"})
    assert not r["ok"]


@pytest.mark.asyncio
async def test_servidor_ainda_nao_existe(ligado):
    ex = JobToolExecutor(db=_DB(), job_manager=_JM(), user_id=uuid.uuid4(), session_id=None)
    r = await ex._claude_task({"objetivo": "x", "onde": "servidor"})
    assert not r["ok"] and "F2" in r["error"]


@pytest.mark.asyncio
async def test_flag_desligada_recusa():
    ex = JobToolExecutor(db=_DB(), job_manager=_JM(), user_id=uuid.uuid4(), session_id=None)
    r = await ex._claude_task({"objetivo": "x"})
    assert not r["ok"] and "desligado" in r["error"]


# ── entrega ──────────────────────────────────────────────────────────────

def test_pausa_por_permissao_entrega_as_acoes_literais_e_como_continuar():
    job = SimpleNamespace(id=uuid.uuid4(), title="Claude: x", extra_metadata={})
    desfecho = Desfecho(
        status="waiting",
        resumo="O Claude parou porque precisa da sua permissão.",
        recusas=[Recusa("Bash", {"command": "npm install"}), Recusa("Write", {"file_path": "/etc/hosts"})],
        session_id="sess-1",
        cwd="/Users/d/proj",
        rodadas=1,
        cota=Cota(janela_5h=0.17),
    )
    t = _terminal(job, {}, "mac-1", desfecho, [], {"claude_mensagens": [{"texto": "oi"}]})
    assert t["status"] == "waiting"
    itens = t["extra_metadata"]["entrega"]["itens"]
    assert "• rodar `npm install`" in itens
    assert "• Write em `/etc/hosts` (não dá para liberar daqui)" in itens
    assert "Para abrir no terminal: `cd /Users/d/proj && claude --resume sess-1`" in itens
    assert "claude_reply(job_id=" in t["result_summary"] and "liberar=true" in t["result_summary"]
    assert "17% da janela de 5 h" in t["result_summary"]
    # o que já estava no extra_metadata sobrevive (o `_settle` sobrescreve a coluna)
    assert t["extra_metadata"]["claude_mensagens"] == [{"texto": "oi"}]
    # o texto da pausa já é do orquestrador, escrito para o Daniel: vai como está
    assert t["extra_metadata"]["entrega"]["literal"] is True
    assert t["extra_metadata"]["entrega"]["fecho"] == "O Claude parou porque precisa da sua permissão."


def test_relatorio_concluido_vai_inteiro_e_os_arquivos_dizem_onde_estao():
    """Job 8f83b37c: o redator reduziu o relatório a "Arquivos salvos e disponíveis"."""
    job = SimpleNamespace(id=uuid.uuid4(), title="Claude: x", extra_metadata={})
    resumo = "Criei o jogo. Não testei o modo de dois jogadores nem o reinício."
    desfecho = Desfecho(status="completed", resumo=resumo, session_id="s", cwd="/Users/d/p", rodadas=1)
    arquivos = [{"file_id": "f1", "path": "jogo.py", "truncado": False}]
    t = _terminal(job, {}, "mac-1", desfecho, arquivos, {})
    entrega = t["extra_metadata"]["entrega"]
    assert entrega["literal"] is True
    assert entrega["fecho"] == f"O Claude terminou. O relatório dele:\n\n{resumo}"
    assert "file_stage(file_id=" in t["result_summary"] and "NÃO no /workspace" in t["result_summary"]


# ── roteador ─────────────────────────────────────────────────────────────

def _router():
    from core.router.intent_router import IntentRouter

    return object.__new__(IntentRouter)


@pytest.mark.parametrize("mensagem", [
    "roda o claude no meu mac pra consertar o teste do neo-panel",
    "faz isso com o Claude Code",
    "pede pro claude refatorar esse módulo",
])
def test_pedido_do_claude_vai_ao_cerebro_e_nao_ao_terminal(mensagem, monkeypatch):
    from shared import IntentType, RoutingDecision

    monkeypatch.setattr("shared.config.get_settings", lambda: SimpleNamespace(claude_code_enabled=True))
    decisao = RoutingDecision(intent=IntentType.CLIENT_TOOL, confidence=0.6, explanation="t",
                              tool_name="run_command", tool_arguments={"command": "claude -p x"})
    decisao = _router()._bias_claude_code(decisao, mensagem)
    assert decisao.intent == IntentType.COGNITIVE
    assert decisao.tool_name is None and decisao.tool_arguments is None


def test_sem_a_flag_o_roteador_nao_muda(monkeypatch):
    from shared import IntentType, RoutingDecision

    monkeypatch.setattr("shared.config.get_settings", lambda: SimpleNamespace(claude_code_enabled=False))
    decisao = RoutingDecision(intent=IntentType.CLIENT_TOOL, confidence=0.6, explanation="t")
    assert _router()._bias_claude_code(decisao, "roda o claude").intent == IntentType.CLIENT_TOOL


def test_claudete_nao_e_claude(monkeypatch):
    from shared import IntentType, RoutingDecision

    monkeypatch.setattr("shared.config.get_settings", lambda: SimpleNamespace(claude_code_enabled=True))
    decisao = RoutingDecision(intent=IntentType.SERVER_TOOL, confidence=0.6, explanation="t")
    assert _router()._bias_claude_code(decisao, "manda msg pra claudete").intent == IntentType.SERVER_TOOL


# ── modo de permissão do corpo local ─────────────────────────────────────

def test_corpo_local_usa_auto_por_padrao_e_aceita_accept_edits():
    from core.claude_code import CorpoLocal

    assert CorpoLocal(None, "mac")._b is None
    assert CorpoLocal(None, "mac").flags_de_permissao([])[:4] == [
        "--permission-mode", "auto", "--permission-prompts", "none",
    ]
    estrito = CorpoLocal(None, "mac", modo_permissao="acceptEdits")
    assert estrito.flags_de_permissao(["Bash(npm install)"]) == [
        "--permission-mode", "acceptEdits", "--permission-prompts", "none",
        "--allowedTools", "Bash(npm install)",
    ]
    # nunca pular permissão, mesmo que a config peça
    assert CorpoLocal(None, "mac", modo_permissao="bypassPermissions").modo_permissao == "auto"


# ── a resposta do Daniel com a tarefa esperando ─────────────────────────

def test_aviso_ao_cerebro_traz_pergunta_acoes_e_como_responder():
    from modules.desktop.ai_service import _nota_claude_esperando

    job = _parado(uuid.uuid4())
    job.extra_metadata["claude"]["pergunta"] = "Pode liberar `python3 hello.py`?"
    nota = _nota_claude_esperando(job)
    assert str(job.id) in nota
    assert "O Claude perguntou: Pode liberar `python3 hello.py`?" in nota
    assert "- rodar `npm install`" in nota
    assert "- Write em `/etc/hosts` (não dá para liberar por aqui)" in nota
    assert f'claude_reply(job_id="{job.id}"' in nota
    assert "NÃO execute você mesmo" in nota
    assert _nota_claude_esperando(None) == ""


@pytest.mark.asyncio
async def test_so_a_tarefa_parada_desta_sessao_e_recente_desvia_a_resposta(monkeypatch):
    from datetime import datetime, timedelta

    from modules.desktop.ai_service import AIService

    sessao, outra, user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    def parado(sess, *, kind="claude_code", horas=0):
        j = _parado(user)
        j.kind, j.session_id = kind, sess
        j.finished_at = datetime.utcnow() - timedelta(hours=horas)
        return j

    class DB:
        def __init__(self, *jobs):
            self.jobs = list(jobs)

        async def list_agent_jobs(self, user_id, *, statuses=None, limit=20):
            return [j for j in self.jobs if j.status in (statuses or [])]

    svc = object.__new__(AIService)
    svc.job_manager = SimpleNamespace(_claude_runner=object())
    monkeypatch.setattr("core.claude_code.chave.ligado", lambda: True)

    certo = parado(sessao)
    svc.db = DB(parado(outra), parado(sessao, kind="browser_local"), parado(sessao, horas=13), certo)
    assert await svc._claude_esperando(sessao, user) is certo

    svc.db = DB(parado(outra), parado(sessao, horas=13))
    assert await svc._claude_esperando(sessao, user) is None

    monkeypatch.setattr("core.claude_code.chave.ligado", lambda: False)  # desligado no painel
    svc.db = DB(certo)
    assert await svc._claude_esperando(sessao, user) is None


# ── ajuste numa tarefa já terminada: mesma sessão, mesma pasta ───────────

class _DBComLista(_DB):
    async def list_agent_jobs(self, user_id=None, *, statuses=None, limit=20):
        return [j for j in self.jobs.values() if not statuses or j.status in statuses]


def _terminada(user_id, *, status="completed", rodou=True) -> AgentJob:
    job = _parado(user_id, status=status)
    job.spec_json["claude"] = {"session_id": "sess-1", "sessao_iniciada": rodou, "cwd": "/Users/d/proj"}
    job.extra_metadata["claude"]["rodadas"] = 1 if rodou else 0
    job.extra_metadata["claude"]["recusas"] = []
    return job


@pytest.mark.asyncio
async def test_ajuste_em_tarefa_concluida_retoma_a_mesma_sessao_sem_o_then_antigo(ligado):
    user = uuid.uuid4()
    feita = _terminada(user)
    db = _DBComLista(feita)
    ex = JobToolExecutor(db=db, job_manager=_JM(), user_id=user, session_id=None)
    r = await ex._claude_reply({"job_id": str(feita.id), "mensagem": "põe um placar no jogo"})
    assert r["ok"] and "MESMA sessão" in r["note"]
    novo = db.criados[0]
    assert novo.spec_json["claude"]["session_id"] == "sess-1"
    assert novo.spec_json["claude"]["cwd"] == "/Users/d/proj"
    assert novo.spec_json["tarefa_id"] == "t-1"  # o Mac não pergunta de novo
    assert "tinha terminado (concluída)" in novo.spec_json["instrucao_inicial"]
    assert "põe um placar no jogo" in novo.spec_json["instrucao_inicial"]
    assert "then" not in novo.spec_json  # "resuma" era da tarefa antiga
    assert novo.title == "Claude: consertar o teste (ajuste)"
    assert feita.status == "completed"  # o balão dela continua dizendo como acabou


@pytest.mark.asyncio
async def test_ajuste_em_tarefa_que_nem_comecou_pede_tarefa_nova(ligado):
    user = uuid.uuid4()
    recusada = _terminada(user, status="cancelled", rodou=False)
    db = _DBComLista(recusada)
    ex = JobToolExecutor(db=db, job_manager=_JM(), user_id=user, session_id=None)
    r = await ex._claude_reply({"job_id": str(recusada.id), "mensagem": "tenta de novo"})
    assert not r["ok"] and "nem chegou a começar" in r["error"] and db.criados == []


@pytest.mark.asyncio
async def test_ajuste_espera_a_tarefa_do_claude_que_esta_rodando(ligado):
    user = uuid.uuid4()
    feita, outra = _terminada(user), _parado(user, status="running")
    db = _DBComLista(feita, outra)
    ex = JobToolExecutor(db=db, job_manager=_JM(), user_id=user, session_id=None)
    r = await ex._claude_reply({"job_id": str(feita.id), "mensagem": "mais um ajuste"})
    assert not r["ok"] and str(outra.id) in r["error"] and db.criados == []


@pytest.mark.asyncio
async def test_resposta_a_tarefa_antiga_vai_para_a_ponta_da_cadeia(ligado):
    """A tarefa já seguiu num ajuste que parou esperando: responder à antiga não abre
    um segundo ramo da mesma sessão — vai para a ponta."""
    user = uuid.uuid4()
    feita = _terminada(user)
    ponta = _parado(user)
    ponta.spec_json["continuacao_de"] = str(feita.id)
    db = _DBComLista(feita, ponta)
    ex = JobToolExecutor(db=db, job_manager=_JM(), user_id=user, session_id=None)
    r = await ex._claude_reply({"job_id": str(feita.id), "mensagem": "pode"})
    assert r["ok"]
    assert ponta.status == "continued"  # quem continuou foi a ponta, que estava esperando
    assert db.criados[0].spec_json["continuacao_de"] == str(ponta.id)


def test_aviso_de_tarefa_recente_manda_ajuste_para_a_mesma_sessao():
    from modules.desktop.ai_service import _nota_claude_recente

    job = _terminada(uuid.uuid4())
    nota = _nota_claude_recente(job)
    assert f'claude_reply(job_id="{job.id}"' in nota
    assert "concluída, na pasta /Users/d/proj" in nota
    assert "`claude_task` é só para trabalho NOVO" in nota
    assert _nota_claude_recente(None) == ""


@pytest.mark.asyncio
async def test_tarefa_recente_e_a_mais_nova_desta_sessao_em_ate_7_dias(monkeypatch):
    from datetime import datetime, timedelta

    from modules.desktop.ai_service import AIService

    sessao, outra, user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    def job(sess, dias, *, kind="claude_code"):
        j = _terminada(user)
        j.kind, j.session_id = kind, sess
        j.created_at = datetime.utcnow() - timedelta(days=dias)
        return j

    svc = object.__new__(AIService)
    svc.job_manager = SimpleNamespace(_claude_runner=object())
    monkeypatch.setattr("core.claude_code.chave.ligado", lambda: True)
    nova = job(sessao, 1)
    svc.db = _DBComLista(job(sessao, 3), nova, job(outra, 0), job(sessao, 0, kind="browser_local"), job(sessao, 8))
    assert await svc._claude_recente(sessao, user) is nova
    svc.db = _DBComLista(job(sessao, 8))
    assert await svc._claude_recente(sessao, user) is None



# ── chave do neo-panel ───────────────────────────────────────────────────

class _Prefs:
    def __init__(self, valor=None):
        self.gravado = valor

    async def get_preferences(self, *, user_id=None, service="", category=None):
        if self.gravado is None:
            return []
        return [SimpleNamespace(key="enabled", value=self.gravado)]

    async def upsert_preference(self, *, category, key, value, user_id=None, service="", source_message=""):
        assert (category, key, user_id, service) == ("claude-code", "enabled", None, "neo")
        self.gravado = value


@pytest.mark.asyncio
async def test_chave_do_painel_vence_o_env_e_vale_na_hora(monkeypatch):
    from core.claude_code import chave

    monkeypatch.setattr("shared.config.get_settings", lambda: SimpleNamespace(claude_code_enabled=True))
    chave._esquecer()
    try:
        assert await chave.carregar(_Prefs()) is True and chave.origem() == "env"
        db = _Prefs()
        await chave.gravar(db, False)
        assert chave.ligado() is False and chave.origem() == "painel" and db.gravado == "false"
        # boot seguinte: o que o painel gravou continua mandando
        chave._esquecer()
        assert await chave.carregar(db) is False
    finally:
        chave._esquecer()


@pytest.mark.asyncio
async def test_desligado_no_painel_o_neo_nao_delega_nem_responde(monkeypatch):
    monkeypatch.setattr("core.claude_code.chave.ligado", lambda: False)
    user = uuid.uuid4()
    feita = _terminada(user)
    ex = JobToolExecutor(db=_DBComLista(feita), job_manager=_JM(), user_id=user, session_id=None)
    for r in (
        await ex._claude_task({"objetivo": "x"}),
        await ex._claude_reply({"job_id": str(feita.id), "mensagem": "ajusta"}),
    ):
        assert not r["ok"] and "neo-panel" in r["error"]


def test_desligado_no_painel_o_roteador_e_o_classificador_esquecem_o_claude(monkeypatch):
    from core.jobs.tools import JOB_TOOLS
    from core.tools.registry import ToolRegistry
    from shared import IntentType, RoutingDecision

    router = _router()
    router.tool_registry = ToolRegistry()
    router.tool_registry.register_many(JOB_TOOLS)

    monkeypatch.setattr("core.claude_code.chave.ligado", lambda: True)
    assert "claude_task" in router._format_tools("server-side")

    monkeypatch.setattr("core.claude_code.chave.ligado", lambda: False)
    decisao = RoutingDecision(intent=IntentType.CLIENT_TOOL, confidence=0.9, explanation="t")
    assert router._bias_claude_code(decisao, "pede pro Claude fazer").intent == IntentType.CLIENT_TOOL
    bloco = router._format_tools("server-side")
    assert "claude_task" not in bloco and "claude_reply" not in bloco
    assert "browser_task" in bloco  # as outras job tools continuam


@pytest.mark.asyncio
async def test_rota_do_painel_le_grava_e_nao_liga_sem_runner(monkeypatch):
    from fastapi import HTTPException

    from api.routes import claude_code_config as rota
    from core.claude_code import chave

    class DB(_Prefs):
        async def list_agent_jobs(self, user_id=None, *, statuses=None, limit=20):
            return [SimpleNamespace(kind="claude_code"), SimpleNamespace(kind="browser_local")]

    monkeypatch.setattr("shared.config.get_settings", lambda: SimpleNamespace(claude_code_enabled=True))
    chave._esquecer()
    try:
        db = DB()
        rota.set_dependencies(db, runner_montado=True)
        estado = await rota.get_config()
        assert estado == {"enabled": True, "source": "env", "env_default": True, "available": True, "active_tasks": 1}
        estado = await rota.update_config(rota.ClaudeCodeConfigUpdate(enabled=False))
        assert estado["enabled"] is False and estado["source"] == "painel" and db.gravado == "false"

        rota.set_dependencies(db, runner_montado=False)
        with pytest.raises(HTTPException) as erro:
            await rota.update_config(rota.ClaudeCodeConfigUpdate(enabled=True))
        assert erro.value.status_code == 409
        # desligar continua permitido sem runner
        assert (await rota.update_config(rota.ClaudeCodeConfigUpdate(enabled=False)))["enabled"] is False
    finally:
        chave._esquecer()
        rota.set_dependencies(None, runner_montado=False)


# ── pasta conferida no mesmo turno e confirmação sem resposta (jobs 21b91d36, 40852ae7) ──

class _Broadcaster:
    def __init__(self, resultado=None, erro=None):
        self.resultado, self.erro, self.pedidos = resultado or {}, erro, []

    async def request(self, device_id, acao, args, timeout=0):
        self.pedidos.append((acao, args))
        if self.erro is not None:
            raise self.erro
        return {"ok": True, "result": self.resultado}


_SAUDAVEL = {"instalado": True, "logado": True, "metodo": "claude.ai", "versao": "2.1.319"}


@pytest.mark.asyncio
async def test_saude_traz_a_pasta_conferida_e_desktop_antigo_nao_quebra():
    from core.claude_code import CorpoLocal

    b = _Broadcaster({**_SAUDAVEL, "pasta": {"ok": False, "motivo": "a pasta não existe: /Users/d/Projetos/x"}})
    s = await CorpoLocal(b, "mac-1").saude(pasta="~/Projetos/x")
    assert s.ok and s.pasta_ok is False and "não existe" in s.pasta_motivo
    assert b.pedidos == [("claude.health", {"pasta": "~/Projetos/x"})]
    # desktop sem a conferência: não sabe responder, e isso não é "não existe"
    s = await CorpoLocal(_Broadcaster(dict(_SAUDAVEL)), "mac-1").saude(pasta="~/Projetos/x")
    assert s.ok and s.pasta_ok is None


@pytest.mark.asyncio
async def test_confirmacao_sem_resposta_diz_o_que_aconteceu():
    from core.claude_code import CorpoIndisponivel, CorpoLocal, PedidoDeRodada
    from core.notifications.device_control import DeviceTimeout

    corpo = CorpoLocal(_Broadcaster(erro=DeviceTimeout("x")), "mac-1")
    with pytest.raises(CorpoIndisponivel) as erro:
        await corpo.lancar(PedidoDeRodada(run_id="r1", cwd="", prompt="x", flags=[], confirmar={}))
    assert "ninguém permitiu no Mac em 3 minutos" in str(erro.value)
    assert "atrás de outra janela" in str(erro.value)


@pytest.mark.asyncio
async def test_pasta_inventada_volta_ao_modelo_no_mesmo_turno(ligado, monkeypatch):
    from core.claude_code import Saude

    class CorpoFalso:
        def __init__(self, *a, **k):
            pass

        async def saude(self, pasta=""):
            return Saude(ok=True, versao="2.1.319", pasta_ok=False, pasta_motivo="a pasta não existe")

    monkeypatch.setattr("core.claude_code.CorpoLocal", CorpoFalso)
    monkeypatch.setattr("core.notifications.device_control.get_device_control_broadcaster", lambda: None)
    user = uuid.uuid4()
    db = _DBComLista()
    ex = JobToolExecutor(db=db, job_manager=_JM(), user_id=user, session_id=None)

    async def mac():
        return "mac-1", ""

    monkeypatch.setattr(ex, "_mac_do_claude", mac)
    r = await ex._claude_task({"objetivo": "pdf", "pasta": "/Users/daniel/Projetos/revolucao-industrial-claude"})
    assert not r["ok"] and "SEM `pasta`" in r["error"] and db.criados == []
    # sem pasta, a tarefa nasce — e o aviso não deixa dizer que já funcionou
    r = await ex._claude_task({"objetivo": "pdf"})
    assert r["ok"] and "ainda NÃO começou" in r["note"]
    assert db.criados[0].spec_json["pasta"] == ""


def test_resumo_de_tarefa_que_nao_rodou_nao_inventa_rodada():
    job = SimpleNamespace(id=uuid.uuid4(), title="Claude: x", extra_metadata={})
    desfecho = Desfecho(status="failed", resumo="Não consegui começar a rodada do Claude: x", rodadas=0)
    t = _terminal(job, {}, "mac-1", desfecho, [], {})
    assert "nenhuma rodada chegou a rodar" in t["result_summary"]
    assert "0 rodada" not in t["result_summary"]


def test_arquivo_binario_que_ficou_no_mac_vai_com_caminho_absoluto():
    """Job 144aa240: o .xlsx não foi recolhido (só texto) e o Neo entregou a planilha DELE."""
    job = SimpleNamespace(id=uuid.uuid4(), title="Claude: x", extra_metadata={})
    desfecho = Desfecho(
        status="completed", resumo="Planilha gerada.", session_id="s",
        cwd="/Users/d/Neo/claude/144aa240", rodadas=1,
        relatorio={"arquivos": ["Populacao_Area_Estados_Brasil.xlsx", "RELATORIO.md", "./gerar.py"]},
    )
    recolhidos = [
        {"file_id": "f1", "path": "RELATORIO.md", "truncado": False},
        {"file_id": "f2", "path": "gerar.py", "truncado": False},
    ]
    t = _terminal(job, {"then": "entregue o xlsx"}, "mac-1", desfecho, recolhidos, {})
    resumo = t["result_summary"]
    assert "Ficaram SÓ no Mac dele" in resumo
    assert "/Users/d/Neo/claude/144aa240/Populacao_Area_Estados_Brasil.xlsx" in resumo
    assert "[[FILE:<caminho>]]" in resumo
    assert "request_user_file" not in resumo  # a continuação não alcança o Mac
    assert "/Users/d/Neo/claude/144aa240/RELATORIO.md" not in resumo
    assert "/Users/d/Neo/claude/144aa240/gerar.py" not in resumo
    # sem `then` não há continuação para ler o aviso
    t2 = _terminal(job, {}, "mac-1", desfecho, [], {})
    assert "Ficaram SÓ no Mac" not in t2["result_summary"]
