"""Nota que diz "baixado em /Users/…" antes de o arquivo existir (job 1c55ba10, 17/11).

O SaveFrom abriu o link do googlevideo como player (preto, 0:00) e nada caiu na
pasta. No passo 8 o modelo anotou "baixado via SaveFrom… destino
/Users/daniel/Downloads/taca-le-pau-marco-veio.mp4"; dali em diante ele leu o
próprio caderno como prova e conferiu a pasta vazia 12 vezes até a trava.

Também: o nome do arquivo da tarefa virou o host `taca-le-pau-marco-veio.mp`.
"""

from __future__ import annotations

import pytest

from core.browser_local.driver import BrowserLocalDriver, _hosts_citados

from .test_driver import _FakeLLM, _FakeProvider

_NOTA_DO_JOB = (
    "Vídeo: 'taca-lê pau nesse carrinho Marco véio' — "
    "https://www.youtube.com/watch?v=nRuiacgrY0w — baixado via SaveFrom, MP4 720p "
    "(itag 398), ~1,6 MB, destino /Users/daniel/Downloads/taca-le-pau-marco-veio.mp4"
)


def _driver() -> BrowserLocalDriver:
    return BrowserLocalDriver(act=None, llm=_FakeLLM(_FakeProvider([])), model="fake")


@pytest.mark.asyncio
async def test_nota_de_baixado_sem_download_visto_e_recusada_com_motivo():
    driver = _driver()
    detalhe = await driver._executar("note", {"texto": _NOTA_DO_JOB})
    assert driver.notes == []
    assert "`downloads` não viu arquivo novo" in detalhe


@pytest.mark.asyncio
async def test_a_mesma_nota_vinda_junto_da_acao_tambem_nao_entra():
    driver = _driver()
    await driver._anotar_junto({"anotar": [_NOTA_DO_JOB]})
    assert driver.notes == []


@pytest.mark.asyncio
async def test_depois_que_o_downloads_viu_arquivo_a_nota_entra():
    driver = _driver()
    driver._viu_download = True
    detalhe = await driver._executar("note", {"texto": _NOTA_DO_JOB})
    assert len(driver.notes) == 1
    assert detalhe.startswith("anotado")


@pytest.mark.parametrize(
    "texto",
    [
        # Dado da página que fala de download, sem afirmar arquivo no disco dele.
        "App Neo — 1,2 mil downloads, nota 4,6",
        "Vídeo: 'taca-lê pau' — disponível no SaveFrom; opções MP4 1080/720/480/360",
        "Formulário salvo com sucesso (mensagem na tela)",
        # Caminho sem afirmar que baixou: é dado (onde algo deveria ir), não promessa.
        "Pasta de destino pedida: /Users/daniel/Downloads",
    ],
)
def test_nota_que_nao_afirma_arquivo_baixado_passa(texto):
    driver = _driver()
    assert driver._guardar_nota(texto) == "nova"


def test_nome_de_arquivo_com_digito_na_extensao_nao_vira_host():
    tarefa = (
        "Salve o arquivo em /Users/daniel/Downloads/ com o nome "
        "taca-le-pau-marco-veio.mp4. Use ssyoutube.com ou www.youtube.com."
    )
    assert _hosts_citados(tarefa) == {"ssyoutube.com", "www.youtube.com"}


@pytest.mark.parametrize("texto", ["toca o clip.mp3 aqui", "o arquivo filme.h264 e o video.m4a"])
def test_outras_extensoes_com_digito_tambem_ficam_de_fora(texto):
    assert _hosts_citados(texto) == set()
