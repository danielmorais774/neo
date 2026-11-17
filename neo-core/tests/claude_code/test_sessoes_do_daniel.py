"""As sessões do Claude que o DANIEL usa: portão, lista, entrega e a volta.

Ver docs/ambiente-de-trabalho-plan.md §9 e §10.
"""

from __future__ import annotations

import uuid

import pytest

from core.authority import NEGAR, PERMITIR, PRINCIPAL_AGENDA, PRINCIPAL_EXTERNO, TurnAuthority, evaluate
from core.jobs.executor import JobToolExecutor, _ficha_para_o_cerebro
from core.jobs.runner_claude_sessao import build_claude_sessao_runner
from core.notifications.device_control import DeviceOffline
from db.models.agent_job import REATTACHABLE_JOB_KINDS, AgentJob

SESSAO = "83b85516-60f9-4365-806e-05d66a81aadd"


# ── portão ───────────────────────────────────────────────────────────────

def test_entregar_numa_sessao_do_daniel_so_em_turno_limpo_dele():
    assert evaluate("claude_enviar", TurnAuthority()).efeito == PERMITIR
    for fonte in ("web_fetch", "email_message", "colagem"):
        autoridade = TurnAuthority()
        autoridade.taint(fonte)
        assert evaluate("claude_enviar", autoridade).efeito == NEGAR, fonte
    for principal in (PRINCIPAL_AGENDA, PRINCIPAL_EXTERNO):
        assert evaluate("claude_enviar", TurnAuthority(principal=principal)).efeito == NEGAR


def test_ver_as_sessoes_e_so_para_o_daniel():
    assert evaluate("claude_sessoes", TurnAuthority()).efeito == PERMITIR
    contaminado = TurnAuthority()
    contaminado.taint("web_fetch")
    # ler a própria lista num turno que leu web não vaza nada: é ele perguntando
    assert evaluate("claude_sessoes", contaminado).efeito == PERMITIR
    for principal in (PRINCIPAL_AGENDA, PRINCIPAL_EXTERNO):
        assert evaluate("claude_sessoes", TurnAuthority(principal=principal)).efeito == NEGAR


def test_as_tools_somem_com_a_chave_e_o_job_sobrevive_a_reinicio():
    from core.claude_code import chave
    from core.jobs import JOB_TOOL_NAMES

    assert {"claude_sessoes", "claude_enviar"} <= set(chave.TOOLS) <= set(JOB_TOOL_NAMES)
    assert "claude_sessao" in REATTACHABLE_JOB_KINDS


# ── executor ─────────────────────────────────────────────────────────────

class _DB:
    def __init__(self) -> None:
        self.criados: list[AgentJob] = []

    async def create_agent_job(self, job):
        self.criados.append(job)
        return job

    async def list_agent_jobs(self, *a, **k):
        return []


class _JM:
    _claude_runner = object()

    def poke(self):
        pass


class _Mac:
    """O canal do Mac: responde por ação e guarda o que recebeu."""

    def __init__(self, **respostas):
        self.respostas = respostas
        self.pedidos: list[tuple[str, dict]] = []

    async def request(self, device_id, acao, args, timeout=30):
        self.pedidos.append((acao, args))
        r = self.respostas[acao]
        if isinstance(r, list):
            r = r.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def is_connected(self, device_id):
        return True


@pytest.fixture
def ligado(monkeypatch):
    from shared.config import get_settings

    monkeypatch.setattr(get_settings(), "claude_code_enabled", True)


@pytest.fixture
def mac(monkeypatch):
    canal = _Mac()

    async def _o_mac(self):
        return "mac-1", ""

    monkeypatch.setattr(JobToolExecutor, "_mac_do_claude", _o_mac)
    monkeypatch.setattr("core.notifications.device_control.get_device_control_broadcaster", lambda: canal)
    return canal


def _executor(db=None):
    return JobToolExecutor(db=db or _DB(), job_manager=_JM(), user_id=uuid.uuid4(), session_id=None)


def _ficha(**campos):
    base = {
        "sessionId": SESSAO, "nome": "neo-a4", "origem": "claude-vscode", "status": "idle", "viva": True,
        "ultimo_uso_min": 190, "titulo": "Teto do raciocínio", "pasta": "~/Documents/projects/neo/neo-core",
        "objetivo": "o PDF falhou", "ultimos_pedidos": ["teto dinâmico"], "ultima_resposta": "Pronto.",
        "arquivos": ["~/x.py"], "artefatos": [], "tarefa_do_neo": False, "vazia": False,
    }
    return {**base, **campos}


@pytest.mark.asyncio
async def test_lista_tira_as_tarefas_do_neo_e_as_abas_vazias(ligado, mac):
    mac.respostas["claude.sessoes"] = {"ok": True, "result": {"sessoes": [
        _ficha(),
        _ficha(sessionId="t", tarefa_do_neo=True),
        _ficha(sessionId="v", vazia=True, titulo=None),
    ]}}
    r = await _executor()._claude_sessoes({})
    assert r["ok"] and [s["sessionId"] for s in r["sessoes"]] == [SESSAO]
    s = r["sessoes"][0]
    assert s["onde"] == "VSCode" and s["estado"] == "aberta" and s["usada"] == "há 3 h"
    assert "nome" not in s and "tarefa_do_neo" not in s, "o que só serve ao Mac não vai ao cérebro"
    # os cortes do neo.toml vão no pedido ao Mac
    acao, args = mac.pedidos[0]
    assert acao == "claude.sessoes" and args["pedidos"] == 3 and args["objetivo_max"] == 160


def test_ficha_fala_a_lingua_do_daniel():
    assert _ficha_para_o_cerebro(_ficha(status="busy", ultimo_uso_min=0, origem="cli"))["estado"] == "aberta, trabalhando agora"
    assert _ficha_para_o_cerebro(_ficha(origem="cli", ultimo_uso_min=0))["usada"] == "agora"
    assert _ficha_para_o_cerebro(_ficha(status="fechada", ultimo_uso_min=4000))["usada"] == "há 2 dias"


@pytest.mark.asyncio
async def test_entrega_numa_aberta_poe_o_job_que_traz_a_resposta(ligado, mac):
    mac.respostas["claude.enviar"] = {"ok": True, "result": {
        "entregue": True, "nome": "neo-a4", "titulo": "Teto do raciocínio", "status": "busy",
        "enviado_em": 1794170000000, "texto_igual": True,
    }}
    db = _DB()
    r = await _executor(db)._claude_enviar({"sessao": SESSAO.upper(), "pedido": "roda os testes do teto"})
    assert r["ok"] and r["sessao"] == "Teto do raciocínio"
    assert "no meio de um trabalho" in r["note"]
    acao, args = mac.pedidos[0]
    assert acao == "claude.enviar" and args["sessionId"] == SESSAO
    assert args["texto"].startswith("roda os testes do teto\n\n")
    assert "Não responda por mensagem" in args["texto"]
    job = db.criados[0]
    assert job.kind == "claude_sessao" and job.notify_policy == "chat"
    assert job.spec_json["enviado_em"] == 1794170000000 and job.spec_json["sessao"] == SESSAO


@pytest.mark.asyncio
async def test_fechada_e_retomada_pelo_orquestrador_na_pasta_dela(ligado, mac):
    mac.respostas["claude.enviar"] = {"ok": True, "result": {
        "entregue": False, "motivo": "sessao_fechada", "pasta": "/Users/d/proj", "titulo": "Plano X",
    }}
    db = _DB()
    r = await _executor(db)._claude_enviar({"sessao": SESSAO, "pedido": "atualiza o plano"})
    assert r["ok"] and r["fechada"]
    job = db.criados[0]
    assert job.kind == "claude_code"
    assert job.spec_json["claude"] == {"session_id": SESSAO, "sessao_iniciada": True, "cwd": "/Users/d/proj"}
    assert job.spec_json["pasta"] == "/Users/d/proj" and "atualiza o plano" in job.spec_json["instrucao_inicial"]


@pytest.mark.asyncio
async def test_pedido_sem_sessao_valida_ou_vazio_nem_vai_ao_mac(ligado, mac):
    ex = _executor()
    assert "sessão inválida" in (await ex._claude_enviar({"sessao": "neo-a4", "pedido": "x"}))["error"]
    assert "vazio" in (await ex._claude_enviar({"sessao": SESSAO, "pedido": "  "}))["error"]
    assert mac.pedidos == []


@pytest.mark.asyncio
async def test_mac_fora_do_ar_vira_frase(ligado, mac):
    mac.respostas["claude.enviar"] = DeviceOffline("x")
    r = await _executor()._claude_enviar({"sessao": SESSAO, "pedido": "x"})
    assert not r["ok"] and "não está conectado" in r["error"]


@pytest.mark.asyncio
async def test_chave_desligada_nao_entrega(monkeypatch, mac):
    from core.claude_code import chave
    from shared.config import get_settings

    chave._esquecer()
    monkeypatch.setattr(get_settings(), "claude_code_enabled", False)
    r = await _executor()._claude_enviar({"sessao": SESSAO, "pedido": "x"})
    assert not r["ok"] and "desligado" in r["error"] and mac.pedidos == []


# ── a volta ──────────────────────────────────────────────────────────────

def _job(enviado_em=1000):
    return AgentJob(
        kind="claude_sessao",
        title="Claude (Teto): roda os testes",
        spec_json={"device_id": "mac-1", "sessao": SESSAO, "titulo": "Teto", "enviado_em": enviado_em},
    )


def _situacao(status, desde, resposta="", viva=True):
    return {"ok": True, "result": {"viva": viva, "status": status, "status_desde_ms": desde,
                                   "resposta": resposta, "respostas": 1 if resposta else 0}}


class _Relogio:
    def __init__(self, passo):
        self.t, self.passo = 0.0, passo

    def __call__(self):
        self.t += self.passo
        return self.t


async def _nao_dorme(_):
    return None


@pytest.mark.asyncio
async def test_volta_quando_a_sessao_fica_livre_depois_da_entrega():
    mac = _Mac(**{"claude.situacao": [
        _situacao("idle", 500),  # ainda o idle de antes da entrega
        _situacao("busy", 1200),
        _situacao("idle", 1300, "Os 12 testes passaram."),
    ]})
    run = build_claude_sessao_runner(get_broadcaster=lambda: mac, dormir=_nao_dorme, relogio=_Relogio(1))
    t = await run(_job())
    assert t["status"] == "completed"
    entrega = t["extra_metadata"]["entrega"]
    assert entrega["literal"] is True
    assert entrega["fecho"] == "A sessão do Claude “Teto” terminou:\n\nOs 12 testes passaram."
    assert mac.pedidos[0][1]["desde_ms"] == 1000


@pytest.mark.asyncio
async def test_sessao_fechada_no_meio_entrega_o_que_ela_escreveu():
    mac = _Mac(**{"claude.situacao": [_situacao("idle", 0, "metade do trabalho", viva=False)]})
    run = build_claude_sessao_runner(get_broadcaster=lambda: mac, dormir=_nao_dorme, relogio=_Relogio(1))
    t = await run(_job())
    assert "foi fechada" in t["extra_metadata"]["entrega"]["fecho"]
    assert "metade do trabalho" in t["extra_metadata"]["entrega"]["fecho"]


@pytest.mark.asyncio
async def test_mac_fora_do_ar_por_tempo_demais_desiste():
    mac = _Mac(**{"claude.situacao": [DeviceOffline("x")] * 50})
    run = build_claude_sessao_runner(get_broadcaster=lambda: mac, dormir=_nao_dorme, relogio=_Relogio(60))
    t = await run(_job())
    assert t["status"] == "failed" and "Perdi o contato" in t["extra_metadata"]["entrega"]["fecho"]


@pytest.mark.asyncio
async def test_sessao_que_nao_termina_tem_prazo():
    mac = _Mac(**{"claude.situacao": [_situacao("busy", 2000)] * 100})
    run = build_claude_sessao_runner(get_broadcaster=lambda: mac, dormir=_nao_dorme, relogio=_Relogio(60))
    t = await run(_job())
    assert t["status"] == "completed"
    assert "ainda está trabalhando" in t["extra_metadata"]["entrega"]["fecho"]
