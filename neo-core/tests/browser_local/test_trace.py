"""O diário de bordo da tarefa no navegador local.

Ele existe para uma coisa só: quando o resultado for "não deu", a investigação
tem que caber num arquivo em vez de meia hora de `grep` no app.log. Então o que
está sob teste aqui é se a EVIDÊNCIA está mesmo lá — o que o modelo viu, o que
decidiu, o que a página respondeu, e o que caiu na pasta de downloads.

E, em segundo lugar, que ele nunca derrube a tarefa: diário é observabilidade,
não caminho crítico.
"""

from __future__ import annotations

import base64
import json

import pytest

from core.browser_local.driver import BrowserLocalDriver
from core.browser_local.trace import RunTrace

# JPEG 1x1 de verdade — o diário decodifica antes de gravar.
_JPEG = (
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
    "AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)


class _Resp:
    def __init__(self, content: str) -> None:
        self._content = content

    def get_content(self) -> str:
        return self._content


class _Provider:
    def __init__(self, steps: list[dict]) -> None:
        self.steps = list(steps)

    async def complete(self, **kwargs):
        step = self.steps.pop(0) if self.steps else {"action": "done", "args": {"answer": "fim"}}
        return _Resp(json.dumps(step))


class _LLM:
    def __init__(self, provider: _Provider) -> None:
        self.provider = provider

    def get_provider_for_model(self, model: str):
        return self.provider


def _snapshot(marker: str = "a"):
    return {
        "url": "https://drive.google.com/drive/my-drive",
        "title": "Meu Drive",
        "elements": [
            {"ref": 0, "tag": "button", "text": "Mais ações", "owner": "Print"},
            {"ref": 1, "tag": "div", "type": "menuitem", "text": "Baixar", "menu": True},
        ],
        "text": f"Print ({marker})",
    }


async def _run(tmp_path, steps, *, marks=True, downloads=None, snapshots=None):
    """Roda o driver com canal dublê e devolve (resultado, texto do diário)."""
    queue = list(snapshots) if snapshots else [_snapshot()]
    entregas = list(downloads or [[], []])

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return queue.pop(0) if len(queue) > 1 else queue[0]
        if action == "browser.screenshot":
            shot = {"ok": True, "data": _JPEG}
            if marks is not None:
                shot["marks"] = marks
            return shot
        if action == "browser.downloads":
            return {"ok": True, "files": entregas.pop(0) if entregas else []}
        return {"ok": True, "label": "Mais ações"}

    trace = RunTrace(tmp_path, "job-1")
    driver = BrowserLocalDriver(
        act=act,
        llm=_LLM(_Provider(steps)),
        model="texto",
        vision_model="visao",
        trace=trace,
    )
    result = await driver.run("baixa a pasta Print")
    return driver, result, (trace.dir / "trace.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_o_diario_guarda_o_que_o_modelo_viu_decidiu_e_o_que_a_pagina_respondeu(tmp_path):
    driver, result, diario = await _run(
        tmp_path,
        [
            {"thought": "o menu está aberto, clico em Baixar", "action": "click", "args": {"ref": 1}},
            {"thought": "pronto", "action": "done", "args": {"answer": "baixei"}},
        ],
        snapshots=[_snapshot("a"), _snapshot("b"), _snapshot("b")],
    )
    assert result.status == "completed"

    # 1. o que viu — a página renderizada, com o menu anunciado
    assert "MENU/DIÁLOGO ABERTO AGORA" in diario
    assert '[1] div(menuitem) "Baixar"' in diario
    # 2. o que decidiu — o thought e a ação, com o modelo e a latência
    assert "o menu está aberto, clico em Baixar" in diario
    # O modelo do passo é o de TEXTO — a visão só aparece quando há `look`.
    assert "`texto`" in diario
    assert '`click` {"ref": 1}' in diario
    # 3. o que a página respondeu
    assert "▶ `click`" in diario and "→ ok" in diario
    # 4. e o desfecho
    assert "## Fim" in diario and "completed" in diario


@pytest.mark.asyncio
async def test_o_jpeg_so_vai_pro_disco_quando_alguem_OLHA(tmp_path):
    """Print virou evento, não rotina.

    Antes era um JPEG por passo — uma tarefa de 20 passos deixava 20 imagens de
    ~500 KB (job 9593f55f) e pagava um modelo de visão em cada uma. Agora só
    existe imagem onde houve `look`, e o diário mostra a PERGUNTA ao lado dela:
    é assim que se audita se a visão está valendo o preço.
    """
    driver, _, diario = await _run(
        tmp_path,
        [
            {"action": "click", "args": {"ref": 0}},
            {"action": "look", "args": {"pergunta": "o menu abriu?"}},
            {"action": "done", "args": {"answer": "fim"}},
        ],
        snapshots=[_snapshot("a"), _snapshot("b"), _snapshot("b")],
    )
    imagens = sorted((tmp_path / "job-1").glob("*-tela.jpg"))
    assert len(imagens) == 1, "uma imagem por `look`, não por passo"
    assert imagens[0].read_bytes() == base64.b64decode(_JPEG)
    assert "com marcações" in diario
    assert "👁 **look**" in diario and "o menu abriu?" in diario


@pytest.mark.asyncio
async def test_print_sem_o_campo_marks_denuncia_desktop_antigo(tmp_path):
    """O device velho nem devolve `marks` — é o sintoma que o diário precisa mostrar."""
    _, _, diario = await _run(
        tmp_path,
        [
            {"action": "look", "args": {"pergunta": "o que tem aqui?"}},
            {"action": "done", "args": {"answer": "fim"}},
        ],
        marks=None,
    )
    assert "desktop antigo?" in diario


@pytest.mark.asyncio
async def test_o_diario_conta_o_que_caiu_na_pasta_de_downloads(tmp_path):
    """O caso "baixou duas vezes": o delta antes/depois entrega sozinho."""
    antes = [{"name": "outro.pdf", "bytes": 10, "seconds_ago": 300, "incomplete": False}]
    depois = antes + [
        {"name": "Print.zip", "bytes": 4096, "seconds_ago": 5, "incomplete": False},
        {"name": "Print (1).zip", "bytes": 4096, "seconds_ago": 2, "incomplete": False},
    ]
    driver, _, diario = await _run(
        tmp_path,
        [{"action": "done", "args": {"answer": "baixei"}}],
        downloads=[antes, depois],
    )
    assert [item["name"] for item in driver.downloaded] == ["Print.zip", "Print (1).zip"]
    assert "downloads (antes da tarefa)" in diario
    assert "downloads (depois da tarefa)" in diario
    assert "a tarefa trouxe 2 arquivo(s)" in diario
    assert "outro.pdf" not in diario.split("a tarefa trouxe")[1]


@pytest.mark.asyncio
async def test_device_sem_a_acao_downloads_e_registrado_sem_quebrar(tmp_path):
    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return _snapshot()
        if action == "browser.screenshot":
            return {"ok": True, "data": _JPEG, "marks": True}
        if action == "browser.downloads":
            raise RuntimeError("ação de navegador desconhecida: browser.downloads")
        return {"ok": True}

    trace = RunTrace(tmp_path, "job-2")
    driver = BrowserLocalDriver(
        act=act,
        llm=_LLM(_Provider([{"action": "done", "args": {"answer": "fim"}}])),
        model="texto",
        trace=trace,
    )
    result = await driver.run("olha o drive")
    assert result.status == "completed", "device sem a ação não pode derrubar a tarefa"
    diario = (trace.dir / "trace.md").read_text(encoding="utf-8")
    assert "não deu para ler" in diario and "desconhecida" in diario


@pytest.mark.asyncio
async def test_recusa_e_falha_do_passo_aparecem_com_o_motivo(tmp_path):
    _, result, diario = await _run(
        tmp_path,
        [
            {"action": "click", "args": {"ref": 99}},  # ref que não existe
            {"action": "done", "args": {"answer": "fim"}},
        ],
    )
    assert "recusado antes de tocar na página" in diario
    assert "não está na página deste passo" in diario


@pytest.mark.asyncio
async def test_diario_quebrado_nunca_derruba_a_tarefa(tmp_path):
    """Observabilidade não é caminho crítico."""

    class _Explode:
        def __getattr__(self, name):
            def boom(*args, **kwargs):
                raise OSError("disco cheio")

            return boom

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return _snapshot()
        return {"ok": True}

    driver = BrowserLocalDriver(
        act=act,
        llm=_LLM(_Provider([{"action": "done", "args": {"answer": "fim"}}])),
        model="texto",
        trace=_Explode(),
    )
    assert (await driver.run("qualquer coisa")).status == "completed"


def test_so_ficam_as_tarefas_recentes(tmp_path):
    from core.browser_local.trace import _KEEP_RUNS

    for i in range(_KEEP_RUNS + 5):
        RunTrace(tmp_path, f"job-{i:03d}").header(tarefa="x")
    assert len(list(tmp_path.iterdir())) == _KEEP_RUNS
