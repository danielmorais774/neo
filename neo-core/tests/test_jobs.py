"""Background jobs — JobManager worker, JobToolExecutor, ProactiveDispatcher.

All driven through fakes (no DB, no Docker): the DB is a small in-memory stub and
the sandbox is a scripted fake. Exercises status mapping, ownership scoping, the
enqueue→poke path, and the dispatcher's persist + publish + idempotency.
"""

import logging
from datetime import datetime
from uuid import UUID, uuid4

import pytest

from core.jobs.dispatcher import ProactiveDispatcher
from core.jobs.executor import JobToolExecutor
from core.jobs.manager import JobManager, _summarize
from core.workbench.sandbox import SandboxResult
from db.models.agent_job import AgentJob
from shared import ToolCall


# ── in-memory DB stub ───────────────────────────────────────────────────────
class _FakeDB:
    def __init__(self):
        self.jobs: dict[UUID, AgentJob] = {}
        self.messages: list[dict] = []
        self.notifications: list[dict] = []

    async def create_agent_job(self, job: AgentJob) -> AgentJob:
        self.jobs[job.id] = job
        return job

    async def get_agent_job(self, job_id: UUID) -> AgentJob | None:
        return self.jobs.get(job_id)

    async def update_agent_job(self, job_id: UUID, **fields) -> AgentJob | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        for k, v in fields.items():
            setattr(job, k, v)
        return job

    async def list_agent_jobs(self, user_id=None, *, statuses=None, limit=20):
        out = [j for j in self.jobs.values() if user_id is None or j.user_id == user_id]
        if statuses:
            out = [j for j in out if j.status in statuses]
        return out[:limit]

    async def save_message(self, **kwargs):
        self.messages.append(kwargs)
        return kwargs

    async def create_notification(self, **kwargs):
        self.notifications.append(kwargs)

        class _N:
            id = uuid4()
            created_at = datetime.utcnow()

        return _N()

    async def get_connected_devices(self, user_id):
        return []


class _FakeSandbox:
    def __init__(self, result: SandboxResult, log_output="log tail"):
        self.result = result
        self.log_output = log_output
        self.ran = []

    async def run(self, command, *, cwd=None, timeout=None):
        self.ran.append((command, cwd, timeout))
        return self.result

    async def read_file(self, path, *, max_bytes=None):
        return SandboxResult(exit_code=0, stdout=self.log_output, stderr="")


def _mk_manager(db, sandbox, dispatcher=None):
    return JobManager(db=db, sandbox=sandbox, dispatcher=dispatcher, tick_interval=1)


# ── JobManager._run_sandbox_task status mapping ─────────────────────────────
@pytest.mark.asyncio
async def test_sandbox_task_completed():
    db = _FakeDB()
    sandbox = _FakeSandbox(SandboxResult(exit_code=0, stdout="", stderr=""), log_output="42")
    jm = _mk_manager(db, sandbox)
    job = AgentJob(kind="sandbox_task", title="calc", spec_json={"command": "echo 42"}, status="running")
    db.jobs[job.id] = job
    terminal = await jm._run_sandbox_task(job)
    assert terminal["status"] == "completed"
    assert terminal["exit_code"] == 0
    assert "42" in terminal["result_summary"]


@pytest.mark.asyncio
async def test_sandbox_task_failed_nonzero_exit():
    db = _FakeDB()
    sandbox = _FakeSandbox(SandboxResult(exit_code=2, stdout="", stderr="boom"), log_output="boom")
    jm = _mk_manager(db, sandbox)
    job = AgentJob(kind="sandbox_task", title="x", spec_json={"command": "false"}, status="running")
    db.jobs[job.id] = job
    terminal = await jm._run_sandbox_task(job)
    assert terminal["status"] == "failed"
    assert terminal["exit_code"] == 2


@pytest.mark.asyncio
async def test_sandbox_task_timeout():
    db = _FakeDB()
    sandbox = _FakeSandbox(SandboxResult(exit_code=137, stdout="", stderr="", timed_out=True))
    jm = _mk_manager(db, sandbox)
    job = AgentJob(kind="sandbox_task", title="x", spec_json={"command": "sleep 999"}, status="running")
    db.jobs[job.id] = job
    terminal = await jm._run_sandbox_task(job)
    assert terminal["status"] == "timeout"


@pytest.mark.asyncio
async def test_run_job_persists_terminal_and_dispatches():
    db = _FakeDB()
    sandbox = _FakeSandbox(SandboxResult(exit_code=0, stdout="", stderr=""), log_output="ok")
    dispatched = []

    class _Disp:
        async def dispatch(self, job):
            dispatched.append(job.id)

    jm = _mk_manager(db, sandbox, dispatcher=_Disp())
    job = AgentJob(kind="sandbox_task", title="x", spec_json={"command": "echo ok"}, status="running")
    db.jobs[job.id] = job
    await jm._run_job(job)
    assert db.jobs[job.id].status == "completed"
    assert db.jobs[job.id].finished_at is not None
    assert dispatched == [job.id]


def test_summarize_includes_status_and_tail():
    s = _summarize("output-here", 0, "completed")
    assert "completed" in s and "output-here" in s
    assert "(sem saída)" in _summarize("", 1, "failed")


# ── JobToolExecutor ─────────────────────────────────────────────────────────
class _FakeManager:
    def __init__(self, sandbox=None):
        self.poked = False
        self._active = {}
        self.sandbox = sandbox

    def poke(self):
        self.poked = True


@pytest.mark.asyncio
async def test_enqueue_creates_queued_job_and_pokes():
    db = _FakeDB()
    jm = _FakeManager()
    uid, sid = uuid4(), uuid4()
    ex = JobToolExecutor(db=db, job_manager=jm, user_id=uid, session_id=sid)
    res = await ex.execute_tool_call(
        ToolCall(id="1", name="neo_bash_background", arguments={"command": "sleep 5", "title": "T"})
    )
    assert res.result["ok"] is True
    assert res.result["status"] == "queued"
    job_id = UUID(res.result["job_id"])
    job = db.jobs[job_id]
    assert job.status == "queued"
    assert job.user_id == uid and job.session_id == sid
    assert job.spec_json["command"] == "sleep 5"
    assert jm.poked is True


@pytest.mark.asyncio
async def test_job_status_and_ownership_scope():
    db = _FakeDB()
    jm = _FakeManager()
    owner, other = uuid4(), uuid4()
    job = AgentJob(user_id=owner, kind="sandbox_task", title="mine", spec_json={}, status="completed",
                   result_summary="done")
    db.jobs[job.id] = job

    mine = JobToolExecutor(db=db, job_manager=jm, user_id=owner, session_id=None)
    res = await mine.execute_tool_call(ToolCall(id="1", name="job_status", arguments={"job_id": str(job.id)}))
    assert res.result["ok"] is True and res.result["status"] == "completed"

    stranger = JobToolExecutor(db=db, job_manager=jm, user_id=other, session_id=None)
    res2 = await stranger.execute_tool_call(ToolCall(id="2", name="job_status", arguments={"job_id": str(job.id)}))
    assert res2.result["ok"] is False  # not visible to a different user


@pytest.mark.asyncio
async def test_job_cancel_marks_cancelled():
    db = _FakeDB()
    jm = _FakeManager()
    uid = uuid4()
    job = AgentJob(user_id=uid, kind="sandbox_task", title="x", spec_json={}, status="running")
    db.jobs[job.id] = job
    ex = JobToolExecutor(db=db, job_manager=jm, user_id=uid, session_id=None)
    res = await ex.execute_tool_call(ToolCall(id="1", name="job_cancel", arguments={"job_id": str(job.id)}))
    assert res.result["ok"] is True
    assert db.jobs[job.id].status == "cancelled"


@pytest.mark.asyncio
async def test_job_cancel_already_terminal():
    db = _FakeDB()
    jm = _FakeManager()
    uid = uuid4()
    job = AgentJob(user_id=uid, kind="sandbox_task", title="x", spec_json={}, status="completed")
    db.jobs[job.id] = job
    ex = JobToolExecutor(db=db, job_manager=jm, user_id=uid, session_id=None)
    res = await ex.execute_tool_call(ToolCall(id="1", name="job_cancel", arguments={"job_id": str(job.id)}))
    assert res.result["ok"] is False


# ── ProactiveDispatcher ─────────────────────────────────────────────────────
class _FakeBroker:
    def __init__(self):
        self.published = []

    async def publish(self, user_id, notif):
        self.published.append((user_id, notif))


@pytest.mark.asyncio
async def test_dispatch_persists_publishes_and_marks_notified():
    db = _FakeDB()
    broker = _FakeBroker()
    disp = ProactiveDispatcher(db=db, broker=broker, llm_router=None)
    uid, sid = uuid4(), uuid4()
    job = AgentJob(user_id=uid, session_id=sid, kind="sandbox_task", title="Converter CSV",
                   spec_json={"command": "python conv.py"}, status="completed",
                   result_summary="[completed] exit=0\n12 linhas", notify_policy="chat")
    db.jobs[job.id] = job
    await disp.dispatch(job)
    # persisted to the origin session's history
    assert db.messages and db.messages[0]["session_id"] == sid
    assert db.messages[0]["role"] == "assistant"
    # published on the broker as a chat_message
    assert broker.published and broker.published[0][1]["kind"] == "chat_message"
    assert broker.published[0][1]["payload"]["job_id"] == str(job.id)
    # marked notified (idempotency)
    assert job.notified_at is not None


@pytest.mark.asyncio
async def test_dispatch_is_idempotent():
    db = _FakeDB()
    broker = _FakeBroker()
    disp = ProactiveDispatcher(db=db, broker=broker, llm_router=None)
    job = AgentJob(user_id=uuid4(), session_id=uuid4(), kind="sandbox_task", title="x",
                   spec_json={}, status="completed", result_summary="done",
                   notify_policy="chat", notified_at=datetime.utcnow())
    db.jobs[job.id] = job
    await disp.dispatch(job)
    assert broker.published == []  # already notified → no re-publish


@pytest.mark.asyncio
async def test_dispatch_silent_policy_skips_delivery():
    db = _FakeDB()
    broker = _FakeBroker()
    disp = ProactiveDispatcher(db=db, broker=broker, llm_router=None)
    job = AgentJob(user_id=uuid4(), session_id=uuid4(), kind="sandbox_task", title="x",
                   spec_json={}, status="completed", result_summary="done", notify_policy="silent")
    db.jobs[job.id] = job
    await disp.dispatch(job)
    assert broker.published == []
    assert db.messages == []
    assert job.notified_at is not None


def test_dispatch_fallback_text_variants():
    disp = ProactiveDispatcher(db=None, broker=None, llm_router=None)
    done = disp._fallback_text(AgentJob(kind="sandbox_task", title="T", spec_json={}, status="completed"), "out")
    assert "Terminei" in done
    fail = disp._fallback_text(AgentJob(kind="sandbox_task", title="T", spec_json={}, status="failed"), "")
    assert "falhou" in fail


# ── a entrega em duas metades: o modelo escreve, o código entrega ───────────
#
# O `_compose` lia até 3.500 caracteres e escrevia no máximo ~1.470 (700 tokens
# de prosa), então todo resultado entre os dois era COMPRIMIDO por um modelo —
# sem aviso e sem registro. Para lista de fatos (preço, nome de arquivo, uuid de
# retomada) parafrasear é a operação errada. Medido em 227 jobs de prod: o corte
# em 3.500 disparou uma vez; o problema nunca foi o corte, era a compressão.


class _ComposeProvider:
    """Guarda o prompt que recebeu — é nele que está o invariante do conserto."""

    def __init__(self, resposta: str = "Terminei de olhar a Amazon pra você."):
        self.resposta = resposta
        self.prompts: list[str] = []

    async def complete(self, *, messages, **kw):
        self.prompts.append("\n".join(m.content for m in messages))
        resposta = self.resposta

        class _R:
            def get_content(self):
                return resposta

        return _R()


class _ComposeLLM:
    def __init__(self, provider):
        self._p = provider

    def get_provider(self, _name):
        return self._p


_ITENS = [
    "pen drive SanDisk Ultra 64GB — R$ 65,76, frete grátis, nota 4,7",
    "pen drive Kingston DataTraveler 64GB — R$ 72,10, vendido por Kabum",
    "pen drive Multilaser Twist 64GB — R$ 51,99, chega quinta",
    "pen drive Sandisk Cruzer 64GB — R$ 58,40, nota 4,5",
]


def _job_com_entrega(**kw):
    base = dict(
        user_id=uuid4(), session_id=uuid4(), kind="browser_local",
        title="Procurar pen drive 64GB", spec_json={"task": "pen drive"},
        status="completed", notify_policy="chat",
        result_summary="Terminei a tarefa.\n\nO que coletei (4) — …:\n"
                       + "\n".join(f"- {i}" for i in _ITENS),
        extra_metadata={"entrega": {"fecho": "Terminei a tarefa.", "itens": list(_ITENS)}},
    )
    base.update(kw)
    return AgentJob(**base)


@pytest.mark.asyncio
async def test_o_redator_nao_ve_os_itens():
    """O invariante de verdade: paráfrase impossível, não proibida.

    Se o bloco entrasse na chamada com um "copie exatamente", estaríamos de
    volta ao defeito de origem — pedir a um modelo que transporte fatos que já
    estão guardados (job 5ab98119). Ele não pode reescrever o que nunca recebeu.
    """
    db, broker = _FakeDB(), _FakeBroker()
    prov = _ComposeProvider()
    disp = ProactiveDispatcher(db=db, broker=broker, llm_router=_ComposeLLM(prov))
    job = _job_com_entrega()
    db.jobs[job.id] = job
    await disp.dispatch(job)
    assert prov.prompts, "o redator nem foi chamado"
    prompt = prov.prompts[0]
    for item in _ITENS:
        assert item not in prompt
    assert "65,76" not in prompt
    # Mas ele sabe que o bloco existe, senão escreve como se nada houvesse.
    assert "4 linhas" in prompt


@pytest.mark.asyncio
async def test_os_itens_chegam_caractere_por_caractere():
    db, broker = _FakeDB(), _FakeBroker()
    disp = ProactiveDispatcher(
        db=db, broker=broker, llm_router=_ComposeLLM(_ComposeProvider())
    )
    job = _job_com_entrega()
    db.jobs[job.id] = job
    await disp.dispatch(job)
    entregue = db.messages[0]["content"]
    assert "Terminei de olhar a Amazon pra você." in entregue  # a prosa
    for item in _ITENS:
        assert item in entregue                                # e os fatos, inteiros


@pytest.mark.asyncio
async def test_sem_redator_os_itens_ainda_chegam_inteiros():
    """O caminho em que a entrega em duas metades vale MAIS.

    O `[:1200]` do fallback cortava justamente quando o redator falhou — 27 de
    227 jobs de prod passam desse tamanho.
    """
    db, broker = _FakeDB(), _FakeBroker()
    disp = ProactiveDispatcher(db=db, broker=broker, llm_router=None)
    job = _job_com_entrega()
    db.jobs[job.id] = job
    await disp.dispatch(job)
    entregue = db.messages[0]["content"]
    assert "Terminei" in entregue
    for item in _ITENS:
        assert item in entregue


@pytest.mark.asyncio
async def test_caderno_grande_chega_sem_perder_item():
    """20 itens entregam 20. É o caso que a compressão silenciosa comia."""
    db, broker = _FakeDB(), _FakeBroker()
    # Do tamanho real de uma nota de caderno: nome, preço, frete, vendedor,
    # nota e URL. É esse tamanho que faz 20 itens passarem do teto de saída.
    muitos = [
        f"pen drive Marca{n:02d} Ultra 64GB — R$ {n}9,90, frete grátis, "
        f"vendido por Loja {n:02d}, nota 4,{n % 10} — "
        f"https://www.amazon.com.br/dp/B0{n:02d}ABCDEFG"
        for n in range(1, 21)
    ]
    disp = ProactiveDispatcher(
        db=db, broker=broker, llm_router=_ComposeLLM(_ComposeProvider())
    )
    job = _job_com_entrega(
        extra_metadata={"entrega": {"fecho": "Achei 20.", "itens": muitos}}
    )
    db.jobs[job.id] = job
    await disp.dispatch(job)
    entregue = db.messages[0]["content"]
    for item in muitos:
        assert item in entregue
    assert len(entregue) > 1470  # acima do que os 700 tokens comportariam


@pytest.mark.asyncio
async def test_itens_nao_sao_colados_duas_vezes():
    """`_fallback_text` não pode juntar o que o `_com_itens` já cola."""
    db, broker = _FakeDB(), _FakeBroker()
    disp = ProactiveDispatcher(db=db, broker=broker, llm_router=None)
    job = _job_com_entrega()
    db.jobs[job.id] = job
    await disp.dispatch(job)
    assert db.messages[0]["content"].count(_ITENS[0]) == 1


@pytest.mark.asyncio
async def test_job_sem_entrega_se_comporta_como_antes():
    """Guarda de regressão para os outros 8 `kind` em produção."""
    db, broker = _FakeDB(), _FakeBroker()
    prov = _ComposeProvider()
    disp = ProactiveDispatcher(db=db, broker=broker, llm_router=_ComposeLLM(prov))
    job = AgentJob(
        user_id=uuid4(), session_id=uuid4(), kind="sandbox_task", title="Converter CSV",
        spec_json={"command": "python conv.py"}, status="completed",
        result_summary="[completed] exit=0\n12 linhas", notify_policy="chat",
    )
    db.jobs[job.id] = job
    await disp.dispatch(job)
    # Caminho de sempre: o resumo inteiro vai ao redator, e nada é colado depois.
    assert "12 linhas" in prov.prompts[0]
    assert db.messages[0]["content"] == prov.resposta


def test_o_corte_do_fallback_deixa_de_ser_mudo(caplog):
    """Até aqui os três cortes da entrega não registravam nada.

    É o que transformava o próximo incidente numa sessão de medição. O kind que
    aparecer neste log é candidato a declarar `entrega`.
    """
    disp = ProactiveDispatcher(db=None, broker=None, llm_router=None)
    job = AgentJob(kind="sandbox_task", title="T", spec_json={}, status="completed")
    with caplog.at_level(logging.WARNING):
        disp._fallback_text(job, "x" * 2000)
    assert "corta o resumo" in caplog.text


# ── Comando adotado (neo_bash que passou do orçamento) e continuação por `then` ──
import asyncio as _asyncio

import core.workbench.executor as _wb_executor


class _RecDispatcher:
    def __init__(self):
        self.jobs: list[AgentJob] = []

    async def dispatch(self, job):
        self.jobs.append(job)


class _WritableSandbox:
    """Sandbox cujo `run` só termina quando o teste manda."""

    def __init__(self, result: SandboxResult):
        self.result = result
        self.release = _asyncio.Event()
        self.cancelled = False
        self.written: dict[str, str] = {}

    async def run(self, command, *, cwd=None, timeout=None, stall_timeout=None):
        try:
            await self.release.wait()
        except _asyncio.CancelledError:
            self.cancelled = True
            raise
        return self.result

    async def write_file(self, path, content):
        self.written[path] = content
        return SandboxResult(exit_code=0, stdout="", stderr="")

    async def read_file(self, path, *, max_bytes=None):
        return SandboxResult(exit_code=0, stdout=self.written.get(path, ""), stderr="")

    def resolve_path(self, path):
        return f"/workspace/{path}"


@pytest.mark.asyncio
async def test_long_neo_bash_is_adopted_as_running_job_and_settles(monkeypatch):
    monkeypatch.setattr(_wb_executor, "_FOREGROUND_BUDGET", 0.05)
    db = _FakeDB()
    sandbox = _WritableSandbox(SandboxResult(exit_code=0, stdout="127 quadros\nRESULT {}", stderr=""))
    dispatcher = _RecDispatcher()
    jm = JobManager(db=db, sandbox=sandbox, dispatcher=dispatcher, tick_interval=1)
    uid, sid = uuid4(), uuid4()
    ex = JobToolExecutor(db=db, job_manager=jm, user_id=uid, session_id=sid)

    res = await ex.execute_tool_call(
        ToolCall(id="1", name="neo_bash", arguments={"command": "python3 ocr_video.py video.mov"})
    )
    assert res.result["backgrounded"] is True
    job = db.jobs[UUID(res.result["job_id"])]
    assert job.status == "running" and job.kind == "sandbox_task"
    assert job.user_id == uid and job.session_id == sid
    assert job.spec_json["adopted"] is True
    assert "NÃO rode o mesmo comando" in res.result["note"]

    sandbox.release.set()
    await jm._active[str(job.id)]
    assert job.status == "completed"
    assert "127 quadros" in job.result_summary
    assert job.log_path.endswith(f"{job.id}.log")
    assert dispatcher.jobs == [job]


@pytest.mark.asyncio
async def test_quick_neo_bash_stays_in_the_foreground(monkeypatch):
    monkeypatch.setattr(_wb_executor, "_FOREGROUND_BUDGET", 5)
    db = _FakeDB()
    sandbox = _WritableSandbox(SandboxResult(exit_code=0, stdout="42\n", stderr=""))
    sandbox.release.set()
    jm = JobManager(db=db, sandbox=sandbox, tick_interval=1)
    ex = JobToolExecutor(db=db, job_manager=jm, user_id=uuid4(), session_id=uuid4())
    res = await ex.execute_tool_call(ToolCall(id="1", name="neo_bash", arguments={"command": "echo 42"}))
    assert res.result["ok"] is True and res.result["stdout"] == "42\n"
    assert not db.jobs


@pytest.mark.asyncio
async def test_cancelling_an_adopted_job_cancels_the_command(monkeypatch):
    monkeypatch.setattr(_wb_executor, "_FOREGROUND_BUDGET", 0.05)
    db = _FakeDB()
    sandbox = _WritableSandbox(SandboxResult(exit_code=0, stdout="", stderr=""))
    jm = JobManager(db=db, sandbox=sandbox, tick_interval=1)
    ex = JobToolExecutor(db=db, job_manager=jm, user_id=uuid4(), session_id=uuid4())
    res = await ex.execute_tool_call(ToolCall(id="1", name="neo_bash", arguments={"command": "sleep 999"}))
    waiter = jm._active[res.result["job_id"]]

    out = await ex.execute_tool_call(ToolCall(id="2", name="job_cancel", arguments={"job_id": res.result["job_id"]}))
    assert out.result["status"] == "cancelled"
    with pytest.raises(_asyncio.CancelledError):
        await waiter
    # O cancelamento do comando é entregue na volta seguinte do laço de eventos.
    for _ in range(20):
        if sandbox.cancelled:
            break
        await _asyncio.sleep(0.01)
    assert sandbox.cancelled is True


@pytest.mark.asyncio
async def test_then_enqueues_a_continuation_turn_and_silences_the_job():
    db = _FakeDB()
    dispatcher = _RecDispatcher()
    jm = JobManager(db=db, sandbox=_WritableSandbox(SandboxResult(0, "", "")), dispatcher=dispatcher, tick_interval=1)
    jm.set_scheduled_runner(lambda job: None)
    job = AgentJob(
        user_id=uuid4(), session_id=uuid4(), kind="sandbox_task", title="OCR do vídeo",
        spec_json={"command": "python3 ocr.py", "then": "entregue o .txt com remetente e horário"},
        status="running",
    )
    db.jobs[job.id] = job

    await jm._settle(job, {"status": "completed", "result_summary": "RESULT {\"linhas\": 412}"})

    continuations = [j for j in db.jobs.values() if j.kind == "scheduled_prompt"]
    assert len(continuations) == 1
    cont = continuations[0]
    assert cont.session_id == job.session_id and cont.user_id == job.user_id
    texto = cont.spec_json["action"]["texto"]
    assert "entregue o .txt com remetente e horário" in texto and "412" in texto
    assert cont.spec_json["continuation_of"] == str(job.id)
    assert job.notify_policy == "silent"
    assert dispatcher.jobs == [job]  # o caderno ainda anota (dispatch roda)


@pytest.mark.asyncio
async def test_then_is_ignored_when_the_job_failed_or_no_runner_exists():
    for status, runner in (("failed", lambda job: None), ("completed", None)):
        db = _FakeDB()
        jm = JobManager(db=db, sandbox=_WritableSandbox(SandboxResult(0, "", "")), dispatcher=_RecDispatcher(), tick_interval=1)
        if runner:
            jm.set_scheduled_runner(runner)
        job = AgentJob(kind="sandbox_task", title="x", spec_json={"then": "faça algo"}, status="running")
        db.jobs[job.id] = job
        await jm._settle(job, {"status": status, "result_summary": "..."})
        assert not [j for j in db.jobs.values() if j.kind == "scheduled_prompt"]
        assert job.notify_policy == "chat"


@pytest.mark.asyncio
async def test_neo_bash_background_stores_then_in_spec():
    db = _FakeDB()
    ex = JobToolExecutor(db=db, job_manager=_FakeManager(), user_id=uuid4(), session_id=uuid4())
    res = await ex.execute_tool_call(
        ToolCall(id="1", name="neo_bash_background", arguments={"command": "x", "title": "T", "then": "monte o arquivo"})
    )
    assert db.jobs[UUID(res.result["job_id"])].spec_json["then"] == "monte o arquivo"


# ── continuação lê a skill do job e entrega arquivo; arquivos chegam ao chat ──
@pytest.mark.asyncio
async def test_continuation_tells_the_turn_to_read_the_job_skill_and_deliver_a_file():
    db = _FakeDB()
    jm = JobManager(db=db, sandbox=_WritableSandbox(SandboxResult(0, "", "")), tick_interval=1)
    job = AgentJob(
        user_id=uuid4(), session_id=uuid4(), kind="sandbox_task", title="OCR",
        spec_json={"command": "python3 /skills/ocr-video/scripts/ocr_video.py v.mov --out x", "then": "entregue o txt"},
        status="running",
    )
    assert await jm._enqueue_continuation(job, "entregue o txt", {"status": "completed", "result_summary": "RESULT {}"})
    cont = next(j for j in db.jobs.values() if j.kind == "scheduled_prompt")
    texto = cont.spec_json["action"]["texto"]
    assert 'skill_read(name="ocr-video")' in texto
    assert "file_save" in texto and "NÃO cole o conteúdo inteiro" in texto


@pytest.mark.asyncio
async def test_dispatch_carries_delivered_files_to_the_chat_and_history():
    db = _FakeDB()
    broker = _FakeBroker()
    disp = ProactiveDispatcher(db=db, broker=broker)
    job = AgentJob(
        user_id=uuid4(), session_id=uuid4(), kind="scheduled_prompt", title="Continuação: OCR",
        spec_json={}, status="completed", result_summary="pronto",
        extra_metadata={
            "texto_literal": "Pronto, montei o arquivo.",
            "files": [{"file_id": "f1", "name": "conversa.txt", "size_bytes": 2269}],
        },
    )
    db.jobs[job.id] = job
    await disp.dispatch(job)
    assert db.notifications[0]["payload"]["files"] == [{"id": "f1", "name": "conversa.txt", "sizeBytes": 2269}]
    assert db.messages[0]["metadata"]["files"][0]["file_id"] == "f1"


class _SessaoComMensagens(_FakeDB):
    """O banco com as respostas que o turno agendado já gravou na sessão."""

    def __init__(self, ja_na_sessao):
        super().__init__()
        self.ja_na_sessao = ja_na_sessao

    async def fetch_session_messages(self, **kwargs):
        from types import SimpleNamespace

        return [SimpleNamespace(content=c, created_at=t) for c, t in self.ja_na_sessao]


def _continuacao_entregue(texto: str, comecou: datetime) -> AgentJob:
    return AgentJob(
        user_id=uuid4(), session_id=uuid4(), kind="scheduled_prompt", title="Continuação: AliExpress",
        spec_json={}, status="completed", result_summary=texto, started_at=comecou,
        extra_metadata={"texto_literal": texto},
    )


@pytest.mark.asyncio
async def test_resposta_que_o_turno_agendado_ja_gravou_nao_e_gravada_de_novo():
    """session-bd44cc03: a resposta da continuação saiu 2× no banco (turno + dispatcher),
    e foi assim em 4 de 4 entregas de `scheduled_prompt` desde 14/09."""
    from datetime import timedelta

    comecou = datetime.utcnow() - timedelta(seconds=15)
    texto = "Concluí a verificação. O anúncio 2 mantém o melhor preço por GB."
    db = _SessaoComMensagens([(texto + "\n", comecou + timedelta(seconds=14))])
    broker = _FakeBroker()
    job = _continuacao_entregue(texto, comecou)
    db.jobs[job.id] = job
    await ProactiveDispatcher(db=db, broker=broker).dispatch(job)
    assert db.messages == []
    # Ao vivo a mensagem continua chegando: só a segunda gravação some.
    assert db.notifications and db.notifications[0]["payload"]


@pytest.mark.asyncio
async def test_mesmo_texto_de_antes_do_job_nao_conta_e_aviso_fixo_e_gravado():
    """"Bom dia" de ontem, igual ao de hoje, não é a cópia deste turno."""
    from datetime import timedelta

    comecou = datetime.utcnow()
    db = _SessaoComMensagens([("Bom dia.", comecou - timedelta(days=1))])
    job = _continuacao_entregue("Bom dia.", comecou)
    db.jobs[job.id] = job
    await ProactiveDispatcher(db=db, broker=_FakeBroker()).dispatch(job)
    assert [m["content"] for m in db.messages] == ["Bom dia."]


@pytest.mark.asyncio
async def test_scheduled_prompt_turn_keeps_the_files_it_delivered():
    from types import SimpleNamespace

    from core.scheduler.runners import _run_prompt

    class _AI:
        async def process_user_input(self, **kwargs):
            yield SimpleNamespace(session_id=None, tool_request=None, command_request=None, is_final=False, message="...", metadata={})
            yield SimpleNamespace(
                session_id=None, tool_request=None, command_request=None, is_final=True,
                message="Pronto.", metadata={"files": [{"file_id": "f1", "name": "c.txt", "size_bytes": 10}]},
            )

    job = AgentJob(user_id=uuid4(), session_id=uuid4(), kind="scheduled_prompt", title="x", spec_json={}, status="running")
    terminal = await _run_prompt(job, {"texto": "faça"}, {}, _AI(), None, None)
    assert terminal["extra_metadata_patch"]["files"][0]["file_id"] == "f1"
    assert terminal["extra_metadata_patch"]["texto_literal"] == "Pronto."
