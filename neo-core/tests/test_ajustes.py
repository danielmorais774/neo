"""shared/ajustes — o arquivo de ajustes nunca derruba o core e sempre documenta tudo."""

from __future__ import annotations

import logging
import os
import tomllib

import pytest

from shared import ajustes as mod
from shared.ajustes import ARQUIVO_PADRAO, Ajustes, ajustes


@pytest.fixture
def arquivo(tmp_path, monkeypatch):
    caminho = tmp_path / "neo.toml"
    monkeypatch.setenv("NEO_AJUSTES", str(caminho))
    monkeypatch.setattr(mod, "_cache", None)
    return caminho


def _escrever(caminho, texto: str) -> None:
    """Escreve e garante mtime novo — dois writes no mesmo tick não mudariam nada."""
    antes = caminho.stat().st_mtime_ns if caminho.exists() else 0
    caminho.write_text(texto, encoding="utf-8")
    os.utime(caminho, ns=(antes + 10**9, antes + 10**9))


def _chaves(dados: dict, prefixo: str = "") -> set[str]:
    out: set[str] = set()
    for chave, valor in dados.items():
        nome = f"{prefixo}{chave}"
        out |= _chaves(valor, f"{nome}.") if isinstance(valor, dict) else {nome}
    return out


def test_o_arquivo_do_repo_documenta_todos_os_ajustes_e_e_valido():
    """Regra da casa: constante nova nasce no modelo E no neo.toml.

    Compara as CHAVES, não os valores: quem adapta o Neo muda os números à
    vontade sem quebrar este teste.
    """
    with ARQUIVO_PADRAO.open("rb") as f:
        dados = tomllib.load(f)
    assert _chaves(dados) == _chaves(Ajustes().model_dump())
    mod.ler(ARQUIVO_PADRAO)


def test_sem_arquivo_vale_o_padrao(arquivo):
    assert ajustes() == Ajustes()


def test_o_arquivo_so_precisa_ter_o_que_muda(arquivo):
    _escrever(arquivo, "[navegador.decisao]\npiso = 3000\n")
    decisao = ajustes().navegador.decisao
    assert decisao.piso == 3000
    assert decisao.maximo == Ajustes().navegador.decisao.maximo
    assert ajustes().teto_adaptativo == Ajustes().teto_adaptativo


def test_editar_vale_sem_reiniciar(arquivo):
    _escrever(arquivo, "[navegador.decisao]\npiso = 3000\n")
    assert ajustes().navegador.decisao.piso == 3000
    _escrever(arquivo, "[navegador.decisao]\npiso = 2500\n")
    assert ajustes().navegador.decisao.piso == 2500


@pytest.mark.parametrize(
    "texto",
    [
        "[navegador.decisao\npiso = 3000\n",  # TOML quebrado
        "[navegador.decisao]\npizo = 3000\n",  # erro de digitação
        "[navegador.decisao]\npiso = 9000\n",  # piso acima do máximo
        "[teto_adaptativo]\npeso_media = 2\n",  # fora da faixa
    ],
)
def test_arquivo_invalido_mantem_a_ultima_versao_boa(arquivo, texto, caplog):
    _escrever(arquivo, "[navegador.decisao]\npiso = 3000\n")
    assert ajustes().navegador.decisao.piso == 3000
    _escrever(arquivo, texto)
    with caplog.at_level(logging.WARNING, logger="shared.ajustes"):
        assert ajustes().navegador.decisao.piso == 3000
    assert "[ajustes]" in caplog.text


def test_arquivo_invalido_desde_o_inicio_cai_no_padrao_e_avisa_uma_vez(arquivo, caplog):
    _escrever(arquivo, "isto não é toml")
    with caplog.at_level(logging.WARNING, logger="shared.ajustes"):
        assert ajustes() == Ajustes()
        assert ajustes() == Ajustes()
    assert caplog.text.count("[ajustes]") == 1
