"""Prints do browser: do base64 do container até a imagem na bolha do chat.

Três costuras, testadas com fakes (sem container, sem File Store real, sem DB):

1. **runner** — ingere os prints do payload no File Store e pendura os
   ``file_id`` no ``extra_metadata`` do job. Print é evidência EXTRA: base64
   podre, store desligado ou quota estourada não podem derrubar a task.
2. **dispatcher** — os marcadores entram DEPOIS do texto redigido pelo LLM (se
   entrassem antes, o "reescreve no seu tom" comeria o marcador).
3. **executor** — a política de print vai no ``spec_json`` da task e o
   ``browser_screenshot`` devolve o marcador pronto para o cérebro copiar.
"""

import base64
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from core.jobs.dispatcher import ProactiveDispatcher
from core.jobs.executor import JobToolExecutor
from core.jobs.runner_browser import build_browser_runner
from core.jobs.screenshots import image_markers, ingest_shots
from db.models.agent_job import AgentJob
from shared import ToolCall

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 64).decode()


def _shot(kind="error", caption="tela do erro", mime="image/png", b64=PNG):
    return {"kind": kind, "caption": caption, "mime": mime, "b64": b64, "url": "https://site/x"}


# ── fakes ───────────────────────────────────────────────────────────────────
class _FakeStore:
    """File Store mínimo: guarda o que recebeu e devolve um id."""

    def __init__(self, raise_on=None):
        self.saved: list[dict] = []
        self.raise_on = raise_on  # índice (1-based) que estoura, simulando quota

    async def ingest_bytes(self, data, **kw):
        if self.raise_on == len(self.saved) + 1:
            raise RuntimeError("quota do store estourada")
        self.saved.append({"data": data, **kw})
        return SimpleNamespace(id=uuid4())


class _FakeDB:
    def __init__(self):
        self.jobs = {}
        self.messages = []
        self.notifications = []

    async def update_agent_job(self, job_id, **fields):
        job = self.jobs.get(job_id)
        for k, v in fields.items():
            setattr(job, k, v)
        return job

    async def get_agent_job(self, job_id):
        return self.jobs.get(job_id)

    async def create_agent_job(self, job):
        self.jobs[job.id] = job
        return job

    async def save_message(self, **kw):
        self.messages.append(kw)
        return kw

    async def create_notification(self, **kw):
        self.notifications.append(kw)
        return None

    async def get_connected_devices(self, user_id):
        return []


class _FakeJM:
    def poke(self):
        pass


def _fake_httpx(payload, status=200, calls=None):
    """Substitui ``httpx.AsyncClient`` por um que sempre devolve ``payload``."""

    class _Resp:
        status_code = status

        def json(self):
            return payload

        @property
        def text(self):
            return str(payload)

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            if calls is not None:
                calls.append({"url": url, "json": json})
            return _Resp()

    return _Client


def _job(**kw):
    base = dict(
        user_id=uuid4(), session_id=uuid4(), kind="browser", title="T",
        spec_json={"task": "abrir o site", "session": "user-default"}, status="running",
    )
    base.update(kw)
    job = AgentJob(**base)
    return job


async def _run(payload, store, monkeypatch, calls=None, spec=None):
    db = _FakeDB()
    job = _job(**({"spec_json": spec} if spec else {}))
    db.jobs[job.id] = job
    monkeypatch.setattr(httpx, "AsyncClient", _fake_httpx(payload, calls=calls))
    runner = build_browser_runner(
        db=db, base_url="http://browser:5100", get_file_store=lambda: store
    )
    return await runner(job), job


# ── runner ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_failed_task_attaches_screenshots(monkeypatch):
    """O caso que motivou a feature: deu erro → o print vem junto com a falha."""
    store = _FakeStore()
    payload = {"ok": False, "error": "timeout depois de 570s",
               "screenshots": [_shot(), _shot(kind="step", caption="passo 3 de 4")]}
    terminal, _job_ = await _run(payload, store, monkeypatch)

    assert terminal["status"] == "failed"
    images = terminal["extra_metadata"]["images"]
    assert len(images) == 2 and all(img["file_id"] for img in images)
    assert "2 prints" in terminal["result_summary"]
    # foi para o catálogo como arquivo temporário de job, não como upload
    assert store.saved[0]["origin"] == "job" and store.saved[0]["retention"] == "temp"
    assert store.saved[0]["source"]["kind"] == "browser_screenshot"


@pytest.mark.asyncio
async def test_success_without_screenshots_keeps_terminal_clean(monkeypatch):
    store = _FakeStore()
    payload = {"ok": True, "success": True, "final_result": "achei", "screenshots": []}
    terminal, _ = await _run(payload, store, monkeypatch)

    assert terminal["status"] == "completed"
    assert "extra_metadata" not in terminal
    assert "📸" not in terminal["result_summary"]
    assert store.saved == []


@pytest.mark.asyncio
async def test_runner_sends_screenshot_policy_from_spec(monkeypatch):
    calls = []
    spec = {"task": "t", "session": "user-default", "screenshots": "always"}
    await _run({"ok": True, "success": True}, _FakeStore(), monkeypatch, calls=calls, spec=spec)
    assert calls[0]["json"]["screenshots"] == "always"
    assert calls[0]["json"]["max_screenshots"] == 3


@pytest.mark.asyncio
async def test_broken_screenshot_never_breaks_the_job(monkeypatch):
    """base64 podre, print gigante e falha do store: a task continua valendo."""
    store = _FakeStore(raise_on=1)
    payload = {
        "ok": True, "success": False, "final_result": "não consegui",
        "screenshots": [
            _shot(b64="isto não é base64!!"),
            _shot(caption="grande demais", b64=base64.b64encode(b"z" * (9 * 1024 * 1024)).decode()),
            _shot(caption="este estoura a quota"),
        ],
    }
    terminal, _ = await _run(payload, store, monkeypatch)

    assert terminal["status"] == "failed"  # veredito da lib, preservado
    assert "extra_metadata" not in terminal  # nenhum print sobreviveu — e tudo bem
    assert "não consegui" in terminal["result_summary"]


@pytest.mark.asyncio
async def test_runner_without_file_store(monkeypatch):
    db = _FakeDB()
    job = _job()
    db.jobs[job.id] = job
    monkeypatch.setattr(httpx, "AsyncClient", _fake_httpx({"ok": False, "error": "x", "screenshots": [_shot()]}))
    runner = build_browser_runner(db=db, base_url="http://browser:5100")  # sem getter
    terminal = await runner(job)
    assert terminal["status"] == "failed" and "extra_metadata" not in terminal


# ── screenshots.py ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ingest_ignores_junk_entries():
    store = _FakeStore()
    images = await ingest_shots(store, [None, {}, {"b64": ""}, _shot()], user_id=None)
    assert len(images) == 1


def test_markers_sanitize_caption():
    """Legenda vem de fora (URL/erro): `]` ou `|` fechariam o marcador no meio."""
    fid = str(uuid4())
    out = image_markers([{"file_id": fid, "caption": "erro em [x] | passo 2"}])
    assert out == [f"[[IMAGE:file:{fid}|erro em [x passo 2]]"]
    assert out[0].count("]]") == 1
    assert image_markers([{"caption": "sem id"}]) == []


# ── dispatcher ──────────────────────────────────────────────────────────────
class _FakeBroker:
    def __init__(self):
        self.published = []

    async def publish(self, user_id, notif):
        self.published.append((user_id, notif))


class _FakeLLM:
    """LLM de composição que ignora o resumo e escreve do seu jeito."""

    def __init__(self, text):
        self.text = text

    def get_provider(self, _name):
        text = self.text

        class _P:
            async def complete(self, **kw):
                return SimpleNamespace(get_content=lambda: text)

        return _P()


async def _dispatch(images, composed="Deu ruim na página de login."):
    db, broker = _FakeDB(), _FakeBroker()
    job = _job(status="failed", result_summary="falhou", notify_policy="chat")
    job.extra_metadata = {"images": images}
    db.jobs[job.id] = job
    disp = ProactiveDispatcher(db=db, broker=broker, llm_router=_FakeLLM(composed))
    await disp.dispatch(job)
    return db, broker


@pytest.mark.asyncio
async def test_markers_survive_the_composing_llm():
    fid = str(uuid4())
    db, broker = await _dispatch([{"file_id": fid, "caption": "tela do erro"}])
    text = db.messages[0]["content"]
    assert "Deu ruim na página de login." in text
    assert f"[[IMAGE:file:{fid}|tela do erro]]" in text
    # o mesmo texto (com marcador) viaja na notificação que o desktop desenha
    assert f"[[IMAGE:file:{fid}" in broker.published[0][1]["payload"]["text"]


@pytest.mark.asyncio
async def test_marker_not_duplicated_when_llm_already_wrote_it():
    fid = str(uuid4())
    db, _ = await _dispatch(
        [{"file_id": fid, "caption": "tela do erro"}],
        composed=f"Olha o erro: [[IMAGE:file:{fid}]]",
    )
    assert db.messages[0]["content"].count(f"file:{fid}") == 1


@pytest.mark.asyncio
async def test_job_without_images_is_untouched():
    db, _ = await _dispatch([])
    assert db.messages[0]["content"] == "Deu ruim na página de login."


# ── executor (tools) ────────────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("arg,expected", [("always", "always"), ("never", "never"),
                                          ("talvez", "auto"), (None, "auto")])
async def test_browser_task_records_screenshot_policy(arg, expected):
    db, jm = _FakeDB(), _FakeJM()
    ex = JobToolExecutor(db=db, job_manager=jm, user_id=uuid4(), session_id=uuid4())
    args = {"task": "abrir o site", "title": "T"}
    if arg is not None:
        args["screenshots"] = arg
    res = await ex.execute_tool_call(ToolCall(id="1", name="browser_task", arguments=args))
    assert res.result["ok"] is True
    job = db.jobs[list(db.jobs)[0]]
    assert job.spec_json["screenshots"] == expected


@pytest.mark.asyncio
async def test_browser_screenshot_returns_ready_marker(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(
        httpx, "AsyncClient",
        _fake_httpx({"ok": True, "warm": True, "current_url": "https://x", "screenshots": [_shot(kind="live")]}),
    )
    ex = JobToolExecutor(
        db=_FakeDB(), job_manager=_FakeJM(), user_id=uuid4(), session_id=uuid4(),
        file_store=store, can_show_images=True,
    )
    res = await ex.execute_tool_call(ToolCall(id="1", name="browser_screenshot", arguments={}))
    assert res.result["ok"] is True
    assert res.result["markers"][0].startswith("[[IMAGE:file:")
    assert store.saved[0]["name"].startswith("browser-print-")


@pytest.mark.asyncio
async def test_browser_screenshot_hides_marker_from_client_that_cannot_render(monkeypatch):
    """WhatsApp/painel leriam `[[IMAGE:file:…]]` como texto cru — melhor nem oferecer."""
    store = _FakeStore()
    monkeypatch.setattr(
        httpx, "AsyncClient",
        _fake_httpx({"ok": True, "warm": True, "current_url": "https://x", "screenshots": [_shot(kind="live")]}),
    )
    ex = JobToolExecutor(  # can_show_images=False (default)
        db=_FakeDB(), job_manager=_FakeJM(), user_id=uuid4(), session_id=uuid4(), file_store=store
    )
    res = await ex.execute_tool_call(ToolCall(id="1", name="browser_screenshot", arguments={}))
    assert res.result["ok"] is True and res.result["markers"] == []
    assert "não exibe imagem" in res.result["note"]
    assert store.saved  # o print continua guardado no catálogo


@pytest.mark.asyncio
async def test_browser_screenshot_cold_session_explains(monkeypatch):
    """Sessão fria não tem tela: melhor dizer isso que devolver quadro preto."""
    monkeypatch.setattr(httpx, "AsyncClient", _fake_httpx({"ok": False, "error": "sessão fria", "warm": False}))
    ex = JobToolExecutor(
        db=_FakeDB(), job_manager=_FakeJM(), user_id=uuid4(), session_id=uuid4(), file_store=_FakeStore()
    )
    res = await ex.execute_tool_call(ToolCall(id="1", name="browser_screenshot", arguments={}))
    assert res.result["ok"] is False and "fria" in res.result["error"]


@pytest.mark.asyncio
async def test_browser_screenshot_without_file_store():
    ex = JobToolExecutor(db=_FakeDB(), job_manager=_FakeJM(), user_id=uuid4(), session_id=uuid4())
    res = await ex.execute_tool_call(ToolCall(id="1", name="browser_screenshot", arguments={}))
    assert res.result["ok"] is False and "File Store" in res.result["error"]


@pytest.mark.asyncio
async def test_job_status_exposes_saved_prints():
    db, jm = _FakeDB(), _FakeJM()
    uid = uuid4()
    fid = str(uuid4())
    job = _job(user_id=uid, status="failed")
    job.extra_metadata = {"images": [{"file_id": fid, "caption": "tela do erro"}]}
    db.jobs[job.id] = job
    ex = JobToolExecutor(db=db, job_manager=jm, user_id=uid, session_id=None)
    res = await ex.execute_tool_call(ToolCall(id="1", name="job_status", arguments={"job_id": str(job.id)}))
    assert res.result["image_markers"] == [f"[[IMAGE:file:{fid}|tela do erro]]"]


# ── falso sucesso: sem veredito e sem nada para mostrar ──────────────────────
@pytest.mark.asyncio
async def test_sem_veredito_e_sem_resultado_nao_e_sucesso(monkeypatch):
    """O job 1ac2277b de 2026-08-28, exatamente como veio.

    `success=None` significa "a lib terminou sem carimbar veredito" — não
    significa "deu certo". Com `final_result` vazio e a sessão parada em
    `about:blank`, o job foi marcado `completed` e o dispatcher escreveu
    "Baixei a pasta Print… Deu tudo certo" para um usuário que não recebeu
    arquivo nenhum.
    """
    store = _FakeStore()
    payload = {
        "ok": True,
        "success": None,
        "final_result": "",
        "current_url": "about:blank",
        "screenshots": [],
    }
    terminal, _ = await _run(payload, store, monkeypatch)
    assert terminal["status"] == "failed"
    assert "sem veredito" in terminal["result_summary"]
    assert "about:blank" in terminal["result_summary"]


@pytest.mark.asyncio
async def test_sem_veredito_mas_com_resposta_continua_valendo(monkeypatch):
    """A lib nem sempre carimba o veredito. Texto entregue é evidência de trabalho."""
    store = _FakeStore()
    payload = {
        "ok": True,
        "success": None,
        "final_result": "O título da página é Exemplo",
        "current_url": "https://exemplo.com/",
        "screenshots": [],
    }
    terminal, _ = await _run(payload, store, monkeypatch)
    assert terminal["status"] == "completed"
    assert "Exemplo" in terminal["result_summary"]


@pytest.mark.asyncio
async def test_sucesso_carimbado_sem_texto_nao_e_afetado(monkeypatch):
    """`success=True` é veredito explícito da lib — não é o caso que a guarda pega."""
    store = _FakeStore()
    payload = {"ok": True, "success": True, "final_result": "", "screenshots": []}
    terminal, _ = await _run(payload, store, monkeypatch)
    assert terminal["status"] == "completed"
