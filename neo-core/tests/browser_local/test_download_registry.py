"""O caminho do download deixa de ser jogado fora.

O device sempre respondeu ``{ok, dir, count, files:[…]}`` a ``browser.downloads``
e o driver ficava só com os ``files`` — então o job sabia o NOME do arquivo e
nunca a PASTA, e o resumo saía em prosa ("na sua pasta de downloads"). No turno
seguinte o classificador tinha que remontar o caminho de cabeça (sessão
081cd1c3).

Duas garantias aqui: a pasta reportada pelo device vira ``path`` em cada item, e
o aviso sai no INSTANTE em que o arquivo aparece — não no fim da tarefa, porque
um job que morre no meio deixa o arquivo no disco do mesmo jeito.
"""

from __future__ import annotations

import json

import pytest

from core.browser_local.driver import BrowserLocalDriver

PASTA = "/Users/daniel/Downloads"


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self._content = content

    def get_content(self) -> str:
        return self._content


class _FakeProvider:
    def __init__(self, steps: list[dict]) -> None:
        self.steps = list(steps)

    async def complete(self, **kwargs):
        step = self.steps.pop(0) if self.steps else {"action": "done", "args": {"answer": "fim"}}
        return _FakeResponse(json.dumps(step))


class _FakeLLM:
    def __init__(self, provider: _FakeProvider) -> None:
        self.provider = provider

    def get_provider_for_model(self, model: str):
        return self.provider


def _snapshot():
    return {
        "url": "https://drive.google.com/",
        "title": "Drive",
        "ready": "complete",
        "stop": False,
        "elements": [{"ref": 0, "tag": "button", "text": "Baixar"}],
        "text": "drive",
    }


def _driver(steps, *, arquivo, com_dir=True, on_download=None):
    """Canal dublê: a pasta começa vazia e ganha ``arquivo`` depois do 1º olhar."""
    estado = {"olhadas": 0}

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 1}
        if action == "browser.snapshot":
            return _snapshot()
        if action == "browser.downloads":
            estado["olhadas"] += 1
            files = [] if estado["olhadas"] == 1 else [dict(arquivo)]
            listing = {"ok": True, "count": len(files), "files": files}
            if com_dir:
                listing["dir"] = PASTA
            return listing
        return {"ok": True}

    return BrowserLocalDriver(
        act=act,
        llm=_FakeLLM(_FakeProvider(steps)),
        model="fake",
        vision_model=None,
        on_download=on_download,
    )


@pytest.mark.asyncio
async def test_pasta_do_device_vira_caminho_no_item() -> None:
    driver = _driver(
        [{"action": "done", "args": {"answer": "baixado"}}],
        arquivo={"name": "nfse_marco.pdf", "bytes": 72643, "incomplete": False},
    )

    await driver.run("baixa o nfs de março")

    assert driver.downloads_dir == PASTA
    assert driver.downloaded[0]["path"] == f"{PASTA}/nfse_marco.pdf"


@pytest.mark.asyncio
async def test_device_antigo_sem_dir_nao_inventa_caminho() -> None:
    """Sem a pasta reportada, o item fica sem ``path`` — melhor que um palpite."""
    driver = _driver(
        [{"action": "done", "args": {"answer": "baixado"}}],
        arquivo={"name": "nfse_marco.pdf", "bytes": 10, "incomplete": False},
        com_dir=False,
    )

    await driver.run("baixa o nfs de março")

    assert driver.downloads_dir == ""
    assert "path" not in driver.downloaded[0]


@pytest.mark.asyncio
async def test_aviso_sai_quando_o_arquivo_aparece_e_nao_no_fim() -> None:
    vistos: list[list[dict]] = []

    async def on_download(items):
        vistos.append([dict(i) for i in items])

    driver = _driver(
        [
            {"action": "downloads", "args": {"wait_s": 0}},
            {"action": "done", "args": {"answer": "baixado"}},
        ],
        arquivo={"name": "nfse_marco.pdf", "bytes": 72643, "incomplete": False},
        on_download=on_download,
    )

    await driver.run("baixa o nfs de março")

    # Primeiro aviso veio da ação `downloads`, no meio da tarefa.
    assert vistos, "nenhum aviso de download foi emitido"
    assert vistos[0][0]["name"] == "nfse_marco.pdf"
    assert vistos[0][0]["path"] == f"{PASTA}/nfse_marco.pdf"


@pytest.mark.asyncio
async def test_mesmo_arquivo_nao_e_anunciado_duas_vezes() -> None:
    """O modelo confere a pasta várias vezes; o registro não pode ganhar
    uma linha por conferência."""
    anunciados: list[str] = []

    async def on_download(items):
        anunciados.extend(str(i.get("name")) for i in items)

    driver = _driver(
        [
            {"action": "downloads", "args": {"wait_s": 0}},
            {"action": "downloads", "args": {"wait_s": 0}},
            {"action": "done", "args": {"answer": "baixado"}},
        ],
        arquivo={"name": "nfse_marco.pdf", "bytes": 72643, "incomplete": False},
        on_download=on_download,
    )

    await driver.run("baixa o nfs de março")

    assert anunciados == ["nfse_marco.pdf"]


@pytest.mark.asyncio
async def test_aviso_sai_mesmo_sem_o_modelo_conferir_a_pasta() -> None:
    """Rede de segurança: o delta do fim da tarefa também anuncia."""
    anunciados: list[str] = []

    async def on_download(items):
        anunciados.extend(str(i.get("name")) for i in items)

    driver = _driver(
        [{"action": "done", "args": {"answer": "baixado"}}],
        arquivo={"name": "nfse_marco.pdf", "bytes": 72643, "incomplete": False},
        on_download=on_download,
    )

    await driver.run("baixa o nfs de março")

    assert anunciados == ["nfse_marco.pdf"]


@pytest.mark.asyncio
async def test_falha_no_aviso_nao_derruba_a_tarefa() -> None:
    async def on_download(items):
        raise RuntimeError("caderno fora do ar")

    driver = _driver(
        [{"action": "done", "args": {"answer": "baixado"}}],
        arquivo={"name": "nfse_marco.pdf", "bytes": 1, "incomplete": False},
        on_download=on_download,
    )

    result = await driver.run("baixa o nfs de março")

    assert result.status == "completed"
