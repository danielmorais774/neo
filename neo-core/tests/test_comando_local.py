"""Comando demorado no computador vira tarefa, e a guarda de repetição responde.

Session-1bbdd7c5 (14/11/2026), limpeza da pasta projeto-loja:

- interação 13: `zip -r projeto-loja.zip projeto-loja/` morreu aos 120 s (o prazo
  fixo do desktop) com 2,6 GB escritos, e a saída do zip (uma linha por arquivo)
  passou de 4 MiB: o servidor recusou a mensagem (`ResourceExhausted`) e o turno caiu.
- interações 5 e 9: o modelo repetiu letra por letra um comando que já tinha a saída
  no transcript, e a guarda encerrou com "Me diga como seguir". O Daniel teve de
  mandar "rola ele de novo" e "tenta de novo". A rodada que repetiu ia a 0.9 de
  temperatura porque a retomada perdia o `onde`.

O desktop (services/local_jobs.rs) adota o comando lento e devolve
`{"neo_tarefa_local": …}`. Aqui se testa o lado do core: reconhecer, abrir o job,
avisar, acompanhar e parar. Sem LLM e sem rede.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.jobs import runner_comando_local as rcl
from core.notifications.device_control import DeviceOffline
from core.planning.plan_manager import ExecutionPlan, PlanStep
from modules.desktop.ai_service import (
    AIService,
    AIServiceResponse,
    ClientActuationRequested,
    IntentType,
    _limites_do_comando,
)

_ZIP = "cd /Users/daniel/Documents/work/ && zip -r projeto-loja.zip projeto-loja/"


def _tarefa(**extra) -> dict:
    t = {
        "id": "t-1",
        "status": "running",
        "command": _ZIP,
        "cwd": "/Users/daniel",
        "elapsed_s": 90,
        "max_runtime_s": 7200,
        "tail": "  adding: projeto-loja/a.txt (stored 0%)\n",
        "stderr_tail": "",
        "log_path": "/Users/daniel/Library/Application Support/neo-desktop/tarefas/t-1.log",
    }
    t.update(extra)
    return t


def _marca(**extra) -> str:
    return json.dumps({"neo_tarefa_local": _tarefa(**extra)})


# ── A marca do desktop ─────────────────────────────────────────────────────


def test_marca_do_desktop_e_reconhecida():
    assert rcl.tarefa_do_resultado(_marca())["id"] == "t-1"


@pytest.mark.parametrize(
    "result",
    [None, "", "total 0\nfoo", '{"neo_tarefa_local": {}}', "neo_tarefa_local mas não é json",
     '{"outra": 1}', '["neo_tarefa_local"]'],
)
def test_saida_comum_nao_e_tarefa(result):
    assert rcl.tarefa_do_resultado(result) is None


@pytest.mark.parametrize(
    ("estado", "status"),
    [
        ({"exit_code": 0}, "completed"),
        ({"exit_code": 12}, "failed"),
        ({"exit_code": None, "signal": 9, "reason": "passou do teto de 2 h e foi encerrada"}, "timeout"),
        ({"exit_code": None, "signal": 9, "reason": "parada pelo Neo"}, "cancelled"),
        ({"exit_code": None, "signal": 9, "reason": "o app do Neo foi fechado"}, "failed"),
    ],
)
def test_fim_da_tarefa_vira_estado_do_job(estado, status):
    fim = rcl.terminal(
        {"status": "exited", "elapsed_s": 312, "tail": "  adding: x\ntotal ok\n", **estado},
        {"command": _ZIP, "cwd": "/Users/daniel"},
    )
    assert fim["status"] == status
    assert _ZIP in fim["result_summary"]
    assert "5 min 12 s" in fim["result_summary"]
    assert "total ok" in fim["result_summary"]
    if estado.get("reason"):
        assert estado["reason"] in fim["result_summary"]


def test_duracao_legivel():
    assert rcl.duracao(45) == "45 s"
    assert rcl.duracao(90) == "1 min 30 s"
    assert rcl.duracao(7200) == "2 h"
    assert rcl.duracao(None) == "0 s"


# ── O runner que acompanha ─────────────────────────────────────────────────


class _Canal:
    """Canal de controle de mentira: devolve as respostas na ordem."""

    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.pedidos: list[tuple[str, dict]] = []

    async def request(self, device_id, action, args=None, timeout=0):
        self.pedidos.append((action, dict(args or {})))
        if action == "tarefa.stop":
            return {"ok": True, "result": {"stopping": True}}
        item = self.respostas.pop(0) if len(self.respostas) > 1 else self.respostas[0]
        if isinstance(item, Exception):
            raise item
        return item


class _Db:
    def __init__(self):
        self.updates: list[dict] = []
        self.criados: list = []

    async def update_agent_job(self, job_id, **kw):
        self.updates.append(kw)
        return SimpleNamespace(id=job_id, notified_at=None, **kw)

    async def create_agent_job(self, job):
        job.id = uuid4()
        self.criados.append(job)
        return job

    async def get_agent_job(self, job_id):
        return None


def _job(**spec) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        spec_json={"device_id": "dev-1", "tarefa_id": "t-1", "command": _ZIP, **spec},
    )


def _rodando(elapsed=100, tail="  adding: a\n  adding: b\n"):
    return {"ok": True, "result": {"status": "running", "elapsed_s": elapsed, "tail": tail}}


@pytest.mark.asyncio
async def test_acompanha_ate_o_fim_e_mostra_o_progresso():
    canal = _Canal([
        _rodando(100),
        _rodando(110),
        {"ok": True, "result": {"status": "exited", "exit_code": 0, "elapsed_s": 300,
                                "tail": "  adding: z\n"}},
    ])
    db = _Db()
    esperas: list[float] = []

    async def dormir(s):
        esperas.append(s)

    fim = await rcl.acompanhar(_job(), db=db, broadcaster=canal, sleep=dormir)

    assert fim["status"] == "completed"
    assert [a for a, _ in canal.pedidos] == ["tarefa.status"] * 3
    # O intervalo dobra enquanto roda (5 → 10), até o teto do neo.toml.
    assert esperas == [5.0, 10.0]
    assert "rodando há 1 min 40 s" in db.updates[0]["progress_note"]
    assert "adding: b" in db.updates[0]["progress_note"]


@pytest.mark.asyncio
async def test_computador_sem_responder_por_tempo_demais_desiste():
    canal = _Canal([DeviceOffline("sem canal")])
    agora = [0.0]

    def relogio():
        agora[0] += 120
        return agora[0]

    async def dormir(_s):
        return None

    fim = await rcl.acompanhar(_job(), db=_Db(), broadcaster=canal, sleep=dormir, clock=relogio)

    assert fim["status"] == "failed"
    assert "sem responder" in fim["result_summary"]


@pytest.mark.asyncio
async def test_app_reiniciado_no_meio_vira_falha_explicada():
    canal = _Canal([{"ok": False, "error": "tarefa desconhecida neste Mac: t-1 (o app pode ter sido reiniciado)"}])
    fim = await rcl.acompanhar(_job(), db=_Db(), broadcaster=canal)
    assert fim["status"] == "failed"
    assert "fechado ou reiniciado" in fim["result_summary"]


@pytest.mark.asyncio
async def test_cancelar_o_job_para_a_tarefa_no_computador():
    canal = _Canal([_rodando()])
    parado = asyncio.Event()

    async def dormir(_s):
        await parado.wait()  # fica rodando até alguém cancelar

    task = asyncio.create_task(rcl.acompanhar(_job(), db=_Db(), broadcaster=canal, sleep=dormir))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ("tarefa.stop", {"id": "t-1"}) in canal.pedidos


@pytest.mark.asyncio
async def test_logs_da_janela_tarefas_vem_ao_vivo_do_computador():
    canal = _Canal([{"ok": True, "result": {"status": "running", "tail": "linha 9\n", "bytes": 99999}}])
    job = SimpleNamespace(status="running", spec_json={"device_id": "d", "tarefa_id": "t"},
                          result_summary=None, progress_note="rodando")
    lido = await rcl.logs(job, 4000, broadcaster=canal)
    assert lido["source"] == "computador" and "linha 9" in lido["logs"]
    assert lido["truncated"] is True
    job.status = "completed"
    job.result_summary = "[completed] exit=0"
    assert (await rcl.logs(job, 4000, broadcaster=canal))["source"] == "resumo"


# ── JobManager ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adocao_abre_job_running_e_fecha_pelo_settle(monkeypatch):
    from core.jobs.manager import JobManager
    from db.models.agent_job import REATTACHABLE_JOB_KINDS

    async def acompanhar_falso(job, *, db):
        return {"status": "completed", "exit_code": 0, "result_summary": "[completed] exit=0\nok"}

    monkeypatch.setattr(rcl, "acompanhar", acompanhar_falso)
    db = _Db()
    jm = JobManager(db=db, sandbox=None)
    uid, sid = uuid4(), uuid4()

    job = await jm.adopt_local_command(user_id=uid, session_id=sid, device_id="dev-1", tarefa=_tarefa())

    assert job.kind == "comando_local" and job.status == "running"
    assert job.spec_json["device_id"] == "dev-1" and job.spec_json["tarefa_id"] == "t-1"
    assert job.notify_policy == "chat"
    assert job.title.startswith("Comando no computador: cd /Users/daniel")
    await asyncio.gather(*list(jm._active.values()))
    assert any(u.get("status") == "completed" for u in db.updates), db.updates
    # Sobrevive a reload do core: volta para a fila e o runner reata.
    assert "comando_local" in REATTACHABLE_JOB_KINDS


# ── O turno que recebe a marca ─────────────────────────────────────────────


class _Sessions:
    def __init__(self):
        self.added: list[dict] = []

    async def add_message(self, **kw):
        self.added.append(kw)

    async def get_history(self, *a, **kw):
        return []

    async def get_or_create_session(self, **kw):
        return SimpleNamespace(id=kw.get("session_id"), user_id=None)


class _Jobs:
    def __init__(self):
        self.adotados: list[dict] = []

    async def adopt_local_command(self, **kw):
        self.adotados.append(kw)
        return SimpleNamespace(id=uuid4())


class _Planner:
    def __init__(self, plan=None):
        self.plan = plan
        self.limpou = False

    def get_active_plan(self, sid):
        return self.plan

    def clear_plans(self, sid):
        self.limpou = True

    def build_plan_update(self, plan):
        return {"status": plan.status, "steps": [s.status for s in plan.steps]}


def _svc(plan=None) -> AIService:
    svc = AIService.__new__(AIService)
    svc.file_store = None
    svc.sessions = _Sessions()
    svc.job_manager = _Jobs()
    svc.planner = _Planner(plan)
    svc._session_attachments = {}
    svc._pending_actuations = {}
    svc._pending_traces = {}
    svc._live_sessions = set()
    svc._image_capable_sessions = set()
    svc._session_registers = {}
    svc._session_disk_evidence = {}
    svc.settings = None
    return svc


async def _resultado(svc, sid, **kw):
    return [
        r async for r in svc.process_tool_result(
            session_id=sid, tool_name=kw.pop("tool_name", "execute_command"), tool_call_id="tc",
            success=True, result=kw.pop("result", _marca()), error=None,
            user_message="vamos transforma em zip", command=_ZIP, **kw,
        )
    ]


@pytest.mark.asyncio
async def test_marca_abre_o_job_e_responde_na_hora():
    svc, sid, uid = _svc(), uuid4(), uuid4()

    out = await _resultado(svc, sid, device_id="dev-1", user_id=uid)

    assert svc.job_manager.adotados[0]["device_id"] == "dev-1"
    assert svc.job_manager.adotados[0]["user_id"] == uid
    assert svc.job_manager.adotados[0]["tarefa"]["id"] == "t-1"
    final = out[-1]
    assert final.is_final
    assert "segundo plano" in final.message and "Tarefas em background" in final.message
    assert "1 min 30 s" in final.message
    assert not any(r.tool_request or r.command_request for r in out)
    assert svc.sessions.added[-1]["content"] == final.message


@pytest.mark.asyncio
async def test_retomada_do_cerebro_e_consumida_e_da_o_computador():
    svc, sid, uid = _svc(), uuid4(), uuid4()
    svc._pending_actuations[str(sid)] = {
        "kind": "shell", "command": _ZIP, "user_id": str(uid),
        "metadata": {"device_id": "dev-2"}, "steps": [],
    }

    await _resultado(svc, sid)

    assert str(sid) not in svc._pending_actuations
    assert svc.job_manager.adotados[0]["device_id"] == "dev-2"
    assert svc.job_manager.adotados[0]["user_id"] == uid


@pytest.mark.asyncio
async def test_no_meio_de_um_plano_os_passos_seguintes_esperam():
    zip_ = PlanStep(index=0, description="compactar a pasta", tool_name="run_command",
                    tool_arguments={"command": _ZIP})
    envio = PlanStep(index=1, description="enviar o zip para o Drive", tool_name="run_command",
                     tool_arguments={"command": "echo"})
    plan = ExecutionPlan(goal="zipar e enviar", steps=[zip_, envio])
    zip_.status = "running"
    svc, sid = _svc(plan), uuid4()

    out = await _resultado(svc, sid, device_id="dev-1", _has_plan=True)

    assert plan.steps[1].status == "skipped"
    assert svc.planner.limpou
    assert any((r.metadata or {}).get("plan_update") for r in out)
    assert "enviar o zip para o Drive" in out[-1].message
    assert not any(r.tool_request for r in out), "o passo 2 não pode rodar antes do zip existir"


@pytest.mark.asyncio
async def test_sem_computador_conhecido_diz_que_nao_vai_avisar():
    svc, sid = _svc(), uuid4()
    out = await _resultado(svc, sid)
    assert not svc.job_manager.adotados
    assert "Não vou conseguir te avisar" in out[-1].message


@pytest.mark.asyncio
async def test_saida_comum_segue_o_caminho_de_sempre():
    svc, sid = _svc(), uuid4()
    seen = []

    async def resume(session_id, **kw):
        seen.append(kw)
        yield AIServiceResponse(intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
                                message="ok", is_final=True)

    svc._resume_after_command = resume
    svc._pending_actuations[str(sid)] = {"kind": "shell", "command": "ls", "steps": []}
    await _resultado(svc, sid, result="total 0\n")
    assert seen and not svc.job_manager.adotados


# ── A guarda de repetição responde com o que já tem ───────────────────────


_DU = "du -sh /Users/daniel/Documents/work/projeto-loja/acme-mobile/android/app/* 2>/dev/null"
_SAIDA_DU = "1,6G\t.../android/app/build\n712K\t.../android/app/src\n"


@pytest.mark.asyncio
async def test_repeticao_literal_responde_com_a_saida_que_o_turno_ja_tinha():
    """Interação 9: du → ls → o MESMO du. A resposta estava no passo 1."""
    svc = _svc()
    pedidos = []

    async def formatar(user_message, command, output, **kw):
        pedidos.append({"user_message": user_message, "output": output, "history": kw.get("history")})
        return "`android/app/build` tem 1,6 GB e é descartável."

    svc._format_tool_result = formatar
    steps = [
        {"command": _DU, "ok": True, "output": _SAIDA_DU, "tool": "run_command"},
        {"command": "ls -lh /x/android/app/build", "ok": True, "output": "intermediates", "tool": "run_command"},
    ]
    car = ClientActuationRequested("run_command", {"command": _DU}, "tc")
    out = [r async for r in svc._emit_client_actuation(
        car, session_id=uuid4(), user_message="checa lá", user_id=None,
        metadata={}, trace=None, slog=None, depth=2, steps=steps,
    )]

    assert not any(r.command_request for r in out), "não podia ter despachado de novo"
    assert out[-1].is_final
    assert out[-1].message == "`android/app/build` tem 1,6 GB e é descartável."
    assert "1,6G" in pedidos[0]["output"] and "intermediates" in pedidos[0]["output"]
    assert pedidos[0]["user_message"] == "checa lá"


@pytest.mark.asyncio
async def test_repeticao_sem_saida_aproveitavel_mantem_a_parada():
    svc = _svc()

    async def formatar(*a, **kw):  # não pode nem ser chamado
        raise AssertionError("sem saída não há o que formatar")

    svc._format_tool_result = formatar
    steps = [{"command": _DU, "ok": False, "output": "", "error": "exit 1", "tool": "run_command"}]
    car = ClientActuationRequested("run_command", {"command": _DU}, "tc")
    out = [r async for r in svc._emit_client_actuation(
        car, session_id=uuid4(), user_message="checa lá", user_id=None,
        metadata={}, trace=None, slog=None, depth=1, steps=steps,
    )]
    assert "Parei antes de repetir" in out[-1].message


@pytest.mark.asyncio
async def test_retomada_usa_a_temperatura_de_oficina():
    """A rodada que lê a saída e escolhe o próximo comando é a de tool, não de conversa."""
    svc = _svc()
    visto = {}

    async def direto(sid, user_message, history, decision, trace, sys_context, **kw):
        visto["decision"] = decision
        yield AIServiceResponse(intent=IntentType.DIRECT_RESPONSE, session_id=sid,
                                message="ok", is_final=True)

    async def vazio(*a, **kw):
        return ""

    svc._handle_direct_response = direto
    svc._build_system_context = lambda md: ""
    svc._build_attachments_manifest = vazio
    svc._file_links_block = vazio
    _ = [r async for r in svc._reenter_cognitive(
        uuid4(), user_message="checa lá", pend={"metadata": {}}, extra_note=None,
        slog=None, trace=None, steps=[],
    )]
    from modules.desktop.ai_service import _ONDE_TAREFA

    assert visto["decision"].onde in _ONDE_TAREFA


# ── Prazos e limites que viajam ────────────────────────────────────────────


def test_prazos_do_neo_toml_vao_para_o_desktop():
    assert _limites_do_comando() == {"background_after_s": 90, "max_runtime_s": 7200}


def test_passo_de_plano_sem_card_leva_os_prazos():
    passo = PlanStep(index=0, description="compactar", tool_name="run_command",
                     tool_arguments={"command": _ZIP})
    plan = ExecutionPlan(goal="zip", steps=[passo])
    svc = _svc(plan)
    svc._fix_command_quoting = lambda command, sid, args: command
    saidas = svc._plan_step_dispatch(plan, passo, session_id=uuid4(), user_message="zip")
    args = json.loads(next(r for r in saidas if r.tool_request).tool_request["arguments"])
    assert args["command"] == _ZIP
    assert args["background_after_s"] == 90 and args["max_runtime_s"] == 7200


def test_servidor_grpc_aceita_mensagem_maior_que_4_mb():
    from grpc_server import _message_size_options

    opcoes = dict(_message_size_options())
    assert opcoes["grpc.max_receive_message_length"] == 16 * 1024 * 1024
    assert opcoes["grpc.max_send_message_length"] == 16 * 1024 * 1024
