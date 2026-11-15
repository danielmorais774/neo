"""Com janela ou sem: a escolha é da CONVERSA, e quem decide é o código.

Sessão 7bc7caa1 (13/11/2026): o Daniel pediu "no meu navegador local, quero ver
acontecendo", o Neo respondeu com uma pergunta, ele respondeu 17 minutos depois
e a tarefa foi para o Brave headless — invisível. O pedido valia 10 minutos; o
cérebro escreveu "com janela visível" só na prosa da tarefa; o resultado disse
"ele vai ver acontecendo na tela dele"; e, quando ele reclamou, o Neo culpou uma
extensão pareada offline.

Pedido dele: conversa que trabalhou no navegador dele com janela usa esse padrão
em todo pedido de navegador, sem depender do modelo decidir.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.browser_local.gate import headless_requested
from core.jobs.executor import JobToolExecutor

_FORM = "https://docs.google.com/forms/d/e/1FAIpQLSfFaF2zgl2RJ2LO9WtEtpQTVkuSwCq_0YkzK5v9zfMm_q3YXg/viewform"


class _DB:
    def __init__(self, *, jobs=None, falas=None) -> None:
        self.jobs = jobs or []
        self.msgs = [
            SimpleNamespace(
                role="user", content=texto, created_at=datetime.utcnow() - timedelta(minutes=minutos)
            )
            for texto, minutos in (falas or [])
        ]
        self.criados = []
        self.por_id = {}

    async def list_agent_jobs(self, user_id, *, statuses=None, limit=20):
        if statuses:
            return [j for j in self.jobs if getattr(j, "status", None) in statuses]
        return sorted(self.jobs, key=lambda j: j.created_at, reverse=True)[:limit]

    async def fetch_session_messages(self, session_id, limit=50, roles=None, **kw):
        return sorted(self.msgs, key=lambda m: m.created_at)[-limit:]

    async def create_agent_job(self, job):
        if getattr(job, "id", None) is None:
            job.id = uuid4()
        self.criados.append(job)
        return job

    async def get_agent_job(self, job_id):
        return self.por_id.get(job_id)


def _executor(mensagem, *, jobs=None, falas=None, session_id=None, device_id="device-1"):
    ex = JobToolExecutor(
        db=_DB(jobs=jobs, falas=falas),
        job_manager=SimpleNamespace(poke=lambda: None),
        user_id=uuid4(),
        session_id=session_id or uuid4(),
        user_message=mensagem,
        device_id=device_id,
    )
    ex._enabled = True
    return ex


def _job(session_id, *, tab, url, minutos, headless=None, status="completed", titulo="t"):
    criado = datetime.utcnow() - timedelta(minutes=minutos)
    spec = {} if headless is None else {"headless": headless}
    return SimpleNamespace(
        id=uuid4(), kind="browser_local", session_id=session_id, status=status, title=titulo,
        created_at=criado, finished_at=criado + timedelta(minutes=1), spec_json=spec,
        result_summary="", user_id=None,
        extra_metadata={"browser_session": {"tab_ref": tab, "url": url, "device_id": "device-1"}},
    )


async def _cria(ex, task="abre o site", **extra):
    resultado = await ex._browser_local_task({"task": task, "title": "t", **extra})
    assert resultado["ok"] is True, resultado
    return resultado, ex.db.criados[-1].spec_json


@pytest.fixture(autouse=True)
def _liga_o_local(monkeypatch):
    from shared.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "browser_local_enabled", True, raising=False)
    monkeypatch.setattr("shared.config.get_settings", lambda: settings)


# ── a cena da 7bc7caa1 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_7bc7caa1_resposta_17_minutos_depois_vai_com_janela():
    ex = _executor(
        "pode abrir o site conferir e responder as ultimas",
        falas=[
            (
                "Preenche esse formulário no meu navegador local, quero ver acontecendo:\n"
                f"{_FORM}\n\nMe avisa aqui do que ele trata assim que entender.",
                17,
            ),
            ("pode abrir o site conferir e responder as ultimas", 0),
        ],
    )
    resultado, spec = await _cria(ex, task=f"Abrir {_FORM} com janela visível e ir à página 9")
    assert spec["headless"] is False
    assert resultado["janela"] is True
    assert "pediu o navegador dele" in spec["motivo_janela"]
    assert "com janela" in resultado["note"]


@pytest.mark.asyncio
async def test_mesmo_site_em_outra_conversa_horas_antes_vai_com_janela():
    """Sem o pedido na conversa, ainda assim: o Forms tinha sido preenchido até a
    página 8 no Edge dele, em outra conversa, 4 h antes. O headless abriu a
    página 1 em branco, sem o login do Google."""
    anterior = _job(uuid4(), tab=1976623515, url=_FORM, minutos=4 * 60)
    ex = _executor("pode abrir o site conferir e responder as ultimas", jobs=[anterior])
    _, spec = await _cria(ex, task=f"ir à página 9 de {_FORM}")
    assert spec["headless"] is False
    assert "de outra conversa" in spec["motivo_janela"]


@pytest.mark.asyncio
async def test_mesmo_site_em_outra_conversa_caduca_no_prazo_do_toml():
    anterior = _job(uuid4(), tab=1976623515, url=_FORM, minutos=13 * 60)
    ex = _executor("responde as últimas", jobs=[anterior])
    _, spec = await _cria(ex, task=f"ir à página 9 de {_FORM}")
    assert spec["headless"] is True


# ── a escolha gruda, e a saída é dele ──────────────────────────────────────


@pytest.mark.asyncio
async def test_tarefa_com_janela_de_dias_atras_na_conversa_segura_a_janela():
    sessao = uuid4()
    anterior = _job(sessao, tab=1976623515, url=_FORM, minutos=3 * 24 * 60)
    ex = _executor("procura o preço do teclado", jobs=[anterior], session_id=sessao)
    _, spec = await _cria(ex, task="preço do teclado em https://www.kabum.com.br")
    assert spec["headless"] is False
    assert "tarefa com janela nesta conversa" in spec["motivo_janela"]


@pytest.mark.asyncio
async def test_job_headless_nao_e_sinal_de_janela():
    """O headless é o padrão de quando ninguém escolheu: contar com ele faria um
    headless errado se perpetuar — e um com janela, também não deve virar o oposto."""
    sessao = uuid4()
    anterior = _job(sessao, tab=-1, url="https://www.kabum.com.br/busca", minutos=5, headless=True)
    ex = _executor("procura o preço do mouse", jobs=[anterior], session_id=sessao)
    _, spec = await _cria(ex, task="preço do mouse em https://www.terabyteshop.com.br")
    assert spec["headless"] is True


@pytest.mark.asyncio
async def test_sem_janela_dito_por_ele_ganha_do_visible_do_cerebro():
    ex = _executor(
        "agora confere o resto",
        falas=[("no meu navegador local, abre o forms", 20), ("daqui pra frente faz sem janela", 5)],
    )
    resultado, spec = await _cria(ex, visible=True)
    assert spec["headless"] is True
    assert resultado["janela"] is False


@pytest.mark.asyncio
async def test_pedir_o_navegador_depois_do_sem_janela_volta_a_janela():
    ex = _executor(
        "agora confere o resto",
        falas=[("faz sem janela", 20), ("abre no meu navegador que eu quero ver", 5)],
    )
    _, spec = await _cria(ex)
    assert spec["headless"] is False


@pytest.mark.asyncio
async def test_a_mais_recente_vence_entre_tarefa_e_fala():
    sessao = uuid4()
    janela_antes = _job(sessao, tab=1976623515, url=_FORM, minutos=30)
    ex = _executor(
        "continua", jobs=[janela_antes], falas=[("pode fazer sem janela", 10)], session_id=sessao
    )
    _, spec = await _cria(ex)
    assert spec["headless"] is True

    janela_depois = _job(sessao, tab=1976623599, url=_FORM, minutos=2)
    ex = _executor(
        "continua", jobs=[janela_antes, janela_depois], falas=[("pode fazer sem janela", 10)],
        session_id=sessao,
    )
    _, spec = await _cria(ex)
    assert spec["headless"] is False


@pytest.mark.asyncio
async def test_sem_janela_na_frase_do_turno_ganha_do_historico():
    ex = _executor(
        "essa pode fazer sem janela", falas=[("no meu navegador local, abre o forms", 3)]
    )
    _, spec = await _cria(ex)
    assert spec["headless"] is True


@pytest.mark.asyncio
async def test_continuacao_automatica_nao_e_fala_dele():
    """"Uma tarefa sua em segundo plano terminou" é o Neo falando, gravado como
    user — não pode virar escolha, nem como frase do turno."""
    continuacao = (
        "[Continuação automática — não é mensagem do usuário. Uma tarefa sua em segundo "
        "plano terminou.] Job: 'x' (job_id=1), estado completed. Sem janela nenhuma."
    )
    ex = _executor(continuacao, falas=[("no meu navegador local, abre o forms", 30), (continuacao, 0)])
    _, spec = await _cria(ex)
    assert spec["headless"] is False


@pytest.mark.asyncio
async def test_banco_fora_nao_derruba_a_tarefa():
    class _Quebrado(_DB):
        async def fetch_session_messages(self, *a, **kw):
            raise RuntimeError("banco fora")

        async def list_agent_jobs(self, *a, **kw):
            if kw.get("statuses"):
                return []
            raise RuntimeError("banco fora")

    ex = _executor("abre o site")
    ex.db = _Quebrado()
    _, spec = await _cria(ex)
    assert spec["headless"] is True


# ── o resultado não mente sobre onde roda ───────────────────────────────────


@pytest.mark.asyncio
async def test_nota_sem_janela_nao_promete_a_tela_dele():
    resultado, spec = await _cria(_executor("procura o preço do teclado"))
    assert spec["headless"] is True
    assert resultado["janela"] is False
    assert "SEM JANELA" in resultado["note"]
    assert "vai ver acontecendo" not in resultado["note"]


@pytest.mark.asyncio
async def test_job_status_diz_que_roda_sem_janela_e_espera_resposta():
    ex = _executor("nao abriu aba nenhuma no meu navegador")
    job = SimpleNamespace(
        id=uuid4(), user_id=None, kind="browser_local", title="Formulário AppDemo",
        status="running", exit_code=None, result_summary="", created_at=datetime.utcnow(),
        finished_at=None, progress_note="esperando você: Como quer que eu preencha o perfil?",
        spec_json={"headless": True, "motivo_janela": "ninguém pediu janela"},
        extra_metadata={"browser_session": {"tab_ref": -1}},
    )
    ex.db.por_id[job.id] = job
    status = await ex._status({"job_id": str(job.id)})
    assert status["janela"] is False
    assert "SEM JANELA" in status["onde"] and "Não é falha de conexão" in status["onde"]
    assert status["por_que"] == "ninguém pediu janela"
    assert status["esperando_resposta"] is True
    assert status["pergunta"] == "Como quer que eu preencha o perfil?"


@pytest.mark.asyncio
async def test_job_status_de_outro_tipo_nao_fala_de_janela():
    ex = _executor("e aí?")
    job = SimpleNamespace(
        id=uuid4(), user_id=None, kind="bash", title="build", status="completed", exit_code=0,
        result_summary="ok", created_at=datetime.utcnow(), finished_at=datetime.utcnow(),
        progress_note=None, spec_json={}, extra_metadata={},
    )
    ex.db.por_id[job.id] = job
    status = await ex._status({"job_id": str(job.id)})
    assert "janela" not in status and "esperando_resposta" not in status


# ── o portão segue a conversa ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_portao_promove_o_servidor_quando_a_conversa_teve_janela():
    sessao = uuid4()
    anterior = _job(sessao, tab=1976623515, url=_FORM, minutos=2 * 24 * 60)
    ex = _executor("pesquisa o preço do teclado", jobs=[anterior], session_id=sessao)
    assert await ex._gate_browser("browser_task") == "browser_local_task"


@pytest.mark.asyncio
async def test_portao_nao_promove_sem_escolha_na_conversa():
    ex = _executor("pesquisa o preço do teclado")
    assert await ex._gate_browser("browser_task") == "browser_task"


# ── o detector do "sem janela" ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "frase",
    [
        "faz sem janela",
        "usa o meu navegador, mas sem janela",
        "pode ser headless",
        "pode rodar headless",
        "sem janela dessa vez",
        "não precisa abrir o navegador, só me traz o preço",
        "não abra a aba, faz por trás",
        "sem abrir o navegador",
        "não quero ver acontecendo",
        "nao quero que abra a janela",
    ],
)
def test_pede_sem_janela(frase):
    assert headless_requested(frase)


@pytest.mark.parametrize(
    "frase",
    [
        # queixas: pedem a JANELA com as mesmas palavras
        "no meu navegador local, acho que vc fez via headless",  # corpus de produção
        "nao abriu aba nenhuma no meu navegador",  # a 7bc7caa1
        "fez sem janela de novo, eu queria ver",
        "abriu sem janela",
        "foi headless de novo",
        "pode abrir no meu navegador? da ultima vez foi sem janela",
        # "não abre" também é "não está abrindo"
        "o navegador não abre",
        "não abre a aba do forms",
        # o vocabulário do próprio Neo e outros sentidos
        "deixa rodando em segundo plano",
        "muda o background do site",
        "a janela do quarto está aberta",
        "não quero que feche a aba",
        "não quero ver isso de novo",
        "Preenche esse formulário no meu navegador local, quero ver acontecendo",
    ],
)
def test_nao_pede_sem_janela(frase):
    assert not headless_requested(frase)


@pytest.mark.asyncio
async def test_headless_cancelado_nao_tapa_o_trabalho_com_janela_no_site():
    """O estado real depois da 7bc7caa1: o headless errado (cancelado) é a tarefa
    mais recente no Forms; atrás dele, três com janela no Edge dele."""
    errado = _job(uuid4(), tab=-1, url=_FORM, minutos=30, headless=True, status="cancelled")
    certo = _job(uuid4(), tab=1976623727, url=_FORM, minutos=4 * 60)
    ex = _executor("pode abrir o forms e me dizer em que página está", jobs=[errado, certo])
    _, spec = await _cria(ex, task=f"abrir {_FORM} e dizer a página")
    assert spec["headless"] is False
    assert "de outra conversa" in spec["motivo_janela"]


@pytest.mark.asyncio
async def test_headless_que_terminou_no_site_segue_sem_janela():
    funcionou = _job(uuid4(), tab=-1, url=_FORM, minutos=30, headless=True, status="completed")
    certo = _job(uuid4(), tab=1976623727, url=_FORM, minutos=4 * 60)
    ex = _executor("pode abrir o forms e me dizer em que página está", jobs=[funcionou, certo])
    _, spec = await _cria(ex, task=f"abrir {_FORM} e dizer a página")
    assert spec["headless"] is True
