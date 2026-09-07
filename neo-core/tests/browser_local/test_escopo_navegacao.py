"""Escopo de navegação: o palpite não pode trancar a tarefa.

Em 2026-09-01 três jobs seguidos morreram em `about:blank` sem carregar UMA
página. O motivo: o nome do arquivo citado na tarefa —
"Palworld-SteamRIP.com.rar" — casou com o regex que extrai domínios do texto e
virou o ÚNICO host autorizado. `drive.google.com` passou a ser "fora do escopo"
e foi recusado quatro vezes, até o driver desistir.

Duas travas contra isso: (1) um nome de arquivo não é host; (2) a primeira
navegação passa sempre, porque antes da primeira página não há de onde vagar.
A lista de domínios PROIBIDOS é outra coisa e continua incondicional.
"""

from __future__ import annotations

import pytest

from core.browser_local.driver import BrowserLocalDriver, _hosts_citados

from tests.browser_local.test_driver_vision import _driver


# ── 1. nome de arquivo não é domínio ────────────────────────────────────


def test_nome_de_arquivo_nao_vira_host():
    tarefa = (
        'No Google Drive, selecione o arquivo "Palworld-SteamRIP.com.rar" e '
        "mova para a lixeira."
    )
    assert _hosts_citados(tarefa) == set(), "foi isto que trancou três jobs"


@pytest.mark.parametrize(
    "texto",
    [
        "manda o backup.tar.gz",
        "abre o relatorio.com.pdf",
        "renomeia notas.com.txt",
        "o instalador setup.com.exe",
    ],
)
def test_outros_arquivos_com_ponto_tambem_ficam_de_fora(texto):
    assert _hosts_citados(texto) == set()


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("entra no drive.google.com e baixa", {"drive.google.com"}),
        ("abre https://github.com/neo/x", {"github.com"}),
        ("vai em www.gov.br pegar a nota", {"www.gov.br"}),
        ("compara americanas.com.br com magalu.com.br",
         {"americanas.com.br", "magalu.com.br"}),
    ],
)
def test_dominio_de_verdade_continua_sendo_reconhecido(texto, esperado):
    assert _hosts_citados(texto) == esperado


# ── 2. a primeira navegação passa sempre ────────────────────────────────


@pytest.mark.asyncio
async def test_primeira_navegacao_passa_mesmo_fora_do_escopo():
    """Reprodução do job cf4f92c9: allowlist errada, agente em about:blank."""
    driver = _driver(
        [
            {"action": "navigate", "args": {"url": "https://drive.google.com"}},
            {"action": "done", "args": {"answer": "cheguei"}},
        ]
    )
    driver._allowed_hosts = {"palworld-steamrip.com.rar"}
    result = await driver.run("remove o Palworld-SteamRIP.com.rar do Drive")
    assert ("browser.navigate", {"url": "https://drive.google.com", "tab_ref": 42}) in (
        driver._acted
    )
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_depois_de_chegar_o_escopo_volta_a_valer():
    """A liberação é só a PRIMEIRA. Chegando, vagar continua barrado."""
    driver = _driver(
        [
            {"action": "navigate", "args": {"url": "https://drive.google.com"}},
            {"action": "navigate", "args": {"url": "https://facebook.com"}},
            {"action": "done", "args": {"answer": "parei"}},
        ]
    )
    driver._allowed_hosts = {"palworld-steamrip.com.rar"}
    result = await driver.run("remove o arquivo do Drive")
    visitados = [
        a["url"] for k, a in driver._acted if k == "browser.navigate"
    ]
    assert visitados == ["https://drive.google.com"], visitados
    assert any("fora do escopo" in (s.detail or "") for s in result.steps)


@pytest.mark.asyncio
async def test_dominio_proibido_nao_ganha_a_cortesia_da_primeira_vez():
    """A allowlist é palpite; a lista de PROIBIDOS é a fronteira de verdade."""
    driver = _driver(
        [
            {"action": "navigate", "args": {"url": "https://banco.com/login"}},
            {"action": "done", "args": {"answer": "parei"}},
        ],
        blocked_domains=("banco.com",),
    )
    result = await driver.run("entra no banco")
    assert not [k for k, _ in driver._acted if k == "browser.navigate"]
    assert any("proibidos" in (s.detail or "") for s in result.steps)


# ── casos tirados do CORPUS de produção ─────────────────────────────────
#
# Medido em 2026-09-01 sobre as 73 tarefas de navegador já executadas: a
# allowlist antiga trancaria 6 delas, e as 6 constam como `failed`. Não era um
# caso isolado do palworld — `app-release.apk` matava jobs desde agosto, pelo
# mesmo motivo e sem ninguém ligar os pontos. O contra-teste também importa: no
# mesmo corpus, ZERO hosts legítimos deixaram de ser reconhecidos.


@pytest.mark.parametrize(
    "tarefa,esperado",
    [
        # job cf4f92c9 — o nome do arquivo virava o único host permitido
        (
            'No Google Drive, localize a pasta "palworld" e selecione o arquivo '
            '"Palworld-SteamRIP.com.rar" e REMOVA-O',
            set(),
        ),
        # jobs c0f2b75b / 9255b2ce / 75ec4f68 — mesmo bug, três semanas antes
        ("Baixar o arquivo app-release.apk do Google Drive para a pasta de downloads", set()),
        ("Baixar o app-relase.apk do seu Google Drive", set()),
        ("Baixar o app-debug.apk e o print.zip", set()),
        # job 6d45a0ec — o pedaço de um E-MAIL não é host, mas os sites são
        (
            "Criar uma conta nova na Conecta (https://www.conecta.example.com/signup). "
            "Use email neo@example.com",
            {"www.conecta.example.com", "example.com"},
        ),
        # job 58e176ba — tarefa que sempre funcionou continua igual
        (
            "Entre em https://news.ycombinator.com e liste o titulo das 3 primeiras noticias.",
            {"news.ycombinator.com"},
        ),
    ],
)
def test_corpus_de_producao(tarefa, esperado):
    assert _hosts_citados(tarefa) == esperado
