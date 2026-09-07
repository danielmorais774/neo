"""A barra de download do NAVEGADOR como sensor — e o que ela desempata.

A leitura de pasta (`read_dir` em ~/Downloads) não distingue as duas coisas que
mais importam depois de um clique em "Baixar":

* o clique não virou download nenhum → tentar de novo é CERTO;
* o servidor ainda está preparando/zipando → tentar de novo DUPLICA.

Nos dois casos a pasta está igual: vazia. Foi assim que o agente cancelou o
próprio download para "tentar de novo" (job 2c8cc496) e entregou 5 cópias de um
APK de 52 MB (job 0f87265f). Com `chrome.downloads` o item existe desde o
instante em que o navegador ACEITA o download — "começou" vira evento, e
"falhou" passa a ser um estado com motivo em vez de silêncio.

O device é dublê: o que está sob teste é o veredito do driver.
"""

from __future__ import annotations

import pytest

from core.browser_local.driver import BrowserLocalDriver

from .test_driver_vision import _FakeLLM, _FakeProvider


def _tela(marker: str = "a"):
    return {
        "url": "https://drive.google.com/drive/my-drive",
        "title": "Meu Drive",
        "ready": "complete",
        "stop": False,
        "elements": [
            {"ref": 0, "tag": "button", "text": "Pesquisar no Google Drive"},
            {"ref": 1, "tag": "button", "text": "Baixar", "owner": "Print"},
            {"ref": 2, "tag": "button", "text": "Fazer o download mesmo assim"},
        ],
        "text": f"Print em Meu Drive ({marker})",
    }


def _item(**over):
    """Um item como o `searchDownloads` da extensão devolve."""
    base = {
        "download_id": "77",
        "name": "Print.zip",
        "path": "/Users/daniel/Downloads/Print.zip",
        "state": "in_progress",
        "error": "",
        "paused": False,
        "bytes": 1024,
        "total_bytes": 5_000_000,
        "incomplete": True,
        "seconds_ago": 1,
    }
    base.update(over)
    return base


def _device(acted, *, downloads, rede=None):
    """`downloads`: lista consumida em ordem (a última se repete para sempre)."""
    fila = list(downloads)
    passo = {"n": 0}

    async def act(action, args, timeout):
        acted.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            passo["n"] += 1
            return _tela(str(passo["n"]))
        if action == "browser.click":
            return {"ok": True, "label": "Baixar"}
        if action == "browser.downloads":
            return fila.pop(0) if len(fila) > 1 else fila[0]
        if action == "browser.network_activity":
            if rede is None:
                raise RuntimeError("device sem termômetro de rede")
            return rede
        return {"ok": True}

    return act


def _driver(acted, passos, **kw):
    return BrowserLocalDriver(
        act=_device(acted, **kw),
        llm=_FakeLLM({}, _FakeProvider(passos)),
        model="texto",
    )


_VAZIO = {"ok": True, "sensor": "browser", "dir": "/Users/daniel/Downloads", "files": []}


@pytest.mark.asyncio
async def test_download_aceito_bloqueia_o_reclique_mesmo_depois_de_conferir():
    """A trava antiga abria com a conferência — e conferir é justamente o que
    descobre que o download está EM CURSO. Com o sensor do navegador o item
    existe antes do primeiro byte, então o segundo clique não pode passar."""
    acted: list[tuple[str, dict]] = []
    driver = _driver(
        acted,
        [
            {"action": "click", "args": {"ref": 2}},
            {"action": "downloads", "args": {"wait_s": 0}},
            {"action": "click", "args": {"ref": 1}},   # duplicaria
            {"action": "done", "args": {"answer": "fim"}},
        ],
        downloads=[_VAZIO, {**_VAZIO, "files": [_item()]}],
    )
    result = await driver.run("baixa a pasta Print")

    cliques = [a for a, _ in acted if a == "browser.click"]
    assert len(cliques) == 1, f"o segundo clique não podia chegar à página: {cliques}"
    recusa = next(s for s in result.steps if s.args.get("recusado"))
    assert "JÁ está baixando agora" in recusa.detail


@pytest.mark.asyncio
async def test_nada_aceito_mas_pagina_carregando_e_preparacao():
    """Pasta vazia + navegador sem item + rede ativa = o site está zipando.
    Mandar baixar de novo aqui é o bug do job 2c8cc496."""
    acted: list[tuple[str, dict]] = []
    driver = _driver(
        acted,
        [
            {"action": "click", "args": {"ref": 2}},
            {"action": "downloads", "args": {"wait_s": 0}},
            {"action": "done", "args": {"answer": "esperando"}},
        ],
        downloads=[_VAZIO],
        # Um recurso terminou de chegar há 200ms: a página está trabalhando.
        rede={"ok": True, "resources": 42, "last_response_end": 9800, "now": 10000},
    )
    result = await driver.run("baixa a pasta Print")

    passo = next(s for s in result.steps if s.action == "downloads")
    assert "AINDA PREPARANDO" in passo.detail
    assert "não clique" in passo.detail.lower()


@pytest.mark.asyncio
async def test_nada_aceito_e_rede_parada_e_clique_perdido():
    """A outra metade do desempate: sem item e sem rede, o clique não pegou —
    e aí insistir no MESMO botão é jogar passo fora."""
    acted: list[tuple[str, dict]] = []
    driver = _driver(
        acted,
        [
            {"action": "click", "args": {"ref": 2}},
            {"action": "downloads", "args": {"wait_s": 0}},
            {"action": "done", "args": {"answer": "não pegou"}},
        ],
        downloads=[_VAZIO],
        # Última resposta de rede foi há 8s: a página está parada.
        rede={"ok": True, "resources": 42, "last_response_end": 2000, "now": 10000},
    )
    result = await driver.run("baixa a pasta Print")

    passo = next(s for s in result.steps if s.action == "downloads")
    assert "O CLIQUE NÃO VIROU DOWNLOAD" in passo.detail


@pytest.mark.asyncio
async def test_falha_passageira_autoriza_uma_nova_tentativa():
    acted: list[tuple[str, dict]] = []
    driver = _driver(
        acted,
        [
            {"action": "click", "args": {"ref": 2}},
            {"action": "downloads", "args": {"wait_s": 0}},
            {"action": "done", "args": {"answer": "falhou"}},
        ],
        downloads=[
            _VAZIO,
            {**_VAZIO, "files": [_item(state="interrupted", error="NETWORK_FAILED")]},
        ],
    )
    result = await driver.run("baixa a pasta Print")

    passo = next(s for s in result.steps if s.action == "downloads")
    assert "DOWNLOAD FALHOU" in passo.detail and "NETWORK_FAILED" in passo.detail
    assert "pode mandar baixar de novo" in passo.detail


@pytest.mark.asyncio
async def test_falha_definitiva_nao_manda_tentar_de_novo():
    """Disco cheio não melhora clicando de novo — repetir só entrega ao usuário
    o mesmo fracasso duas vezes."""
    acted: list[tuple[str, dict]] = []
    driver = _driver(
        acted,
        [
            {"action": "click", "args": {"ref": 2}},
            {"action": "downloads", "args": {"wait_s": 0}},
            {"action": "done", "args": {"answer": "falhou"}},
        ],
        downloads=[
            _VAZIO,
            {**_VAZIO, "files": [_item(state="interrupted", error="FILE_NO_SPACE")]},
        ],
    )
    result = await driver.run("baixa a pasta Print")

    passo = next(s for s in result.steps if s.action == "downloads")
    assert "FILE_NO_SPACE" in passo.detail
    assert "não melhora tentando de novo" in passo.detail


@pytest.mark.asyncio
async def test_device_sem_termometro_de_rede_nao_derruba_o_passo():
    """O termômetro é melhor esforço: device antigo não tem a ação, e o veredito
    tem que cair no comportamento de sempre em vez de estourar o passo."""
    acted: list[tuple[str, dict]] = []
    driver = _driver(
        acted,
        [
            {"action": "click", "args": {"ref": 2}},
            {"action": "downloads", "args": {"wait_s": 0}},
            {"action": "done", "args": {"answer": "nada"}},
        ],
        downloads=[_VAZIO],
        rede=None,   # a ação explode no device
    )
    result = await driver.run("baixa a pasta Print")

    passo = next(s for s in result.steps if s.action == "downloads")
    assert passo.ok, "a ação inteira não pode falhar por causa do termômetro"
    assert "O CLIQUE NÃO VIROU DOWNLOAD" in passo.detail


@pytest.mark.asyncio
async def test_sensor_de_disco_continua_valendo():
    """Device antigo não manda `sensor` nem `state`: nada de veredito novo, e o
    reclique continua liberado pela conferência (contrato de sempre)."""
    acted: list[tuple[str, dict]] = []
    driver = _driver(
        acted,
        [
            {"action": "click", "args": {"ref": 2}},
            {"action": "downloads", "args": {"wait_s": 0}},
            {"action": "click", "args": {"ref": 1}},
            {"action": "done", "args": {"answer": "fim"}},
        ],
        downloads=[{"ok": True, "dir": "/Users/daniel/Downloads", "files": []}],
    )
    result = await driver.run("baixa a pasta Print")

    cliques = [a for a, _ in acted if a == "browser.click"]
    assert len(cliques) == 2, "com o sensor de disco, conferir libera o clique"
    assert not [s for s in result.steps if s.args.get("recusado")]


@pytest.mark.asyncio
async def test_interrompido_nunca_e_lido_como_concluido():
    """Download que morreu no meio deixa um parcial no disco. Ler isso como
    conclusão é o falso sucesso que a leitura de pasta cometia — por isso a
    falha é avaliada ANTES de "pronto", mesmo se `incomplete` vier false."""
    acted: list[tuple[str, dict]] = []
    driver = _driver(
        acted,
        [
            {"action": "click", "args": {"ref": 2}},
            {"action": "downloads", "args": {"wait_s": 0}},
            {"action": "done", "args": {"answer": "falhou"}},
        ],
        downloads=[
            _VAZIO,
            {
                **_VAZIO,
                "files": [
                    _item(state="interrupted", error="SERVER_FORBIDDEN", incomplete=False)
                ],
            },
        ],
    )
    result = await driver.run("baixa a pasta Print")

    passo = next(s for s in result.steps if s.action == "downloads")
    assert "CONCLUÍDO" not in passo.detail
    assert "DOWNLOAD FALHOU" in passo.detail and "SERVER_FORBIDDEN" in passo.detail


@pytest.mark.asyncio
async def test_preparando_bloqueia_o_reclique_com_a_frase_certa():
    """Conferir não pode virar licença para clicar de novo enquanto o site zipa —
    e a recusa não pode dizer "você ainda não conferiu", que seria mentira."""
    acted: list[tuple[str, dict]] = []
    driver = _driver(
        acted,
        [
            {"action": "click", "args": {"ref": 2}},
            {"action": "downloads", "args": {"wait_s": 0}},
            {"action": "click", "args": {"ref": 1}},   # o erro do job 2c8cc496
            {"action": "done", "args": {"answer": "fim"}},
        ],
        downloads=[_VAZIO],
        rede={"ok": True, "resources": 42, "last_response_end": 9800, "now": 10000},
    )
    result = await driver.run("baixa a pasta Print")

    cliques = [a for a, _ in acted if a == "browser.click"]
    assert len(cliques) == 1, f"não podia pedir um segundo arquivo: {cliques}"
    recusa = next(s for s in result.steps if s.args.get("recusado"))
    assert "AINDA ESTÁ MONTANDO" in recusa.detail
    assert "não conferiu" not in recusa.detail
