"""Toda tool declarada no repo tem classificação de risco explícita.

Por que varrer o FONTE em vez de importar o registry: o registry só existe
depois do wiring do ``api/main.py`` (settings, banco, flags), e metade das tools
entra condicionalmente. A varredura pega o conjunto inteiro, inclusive o que
está atrás de flag desligada — que é justamente onde a classificação costuma
faltar quando alguém liga a flag meses depois.

Quando este teste falhar, a correção é uma linha em ``core/authority/risk.py``:
classifique a tool nova. O fail-safe (atuação) existe para não deixar buraco,
não para virar o caminho normal.
"""

from __future__ import annotations

import re
from pathlib import Path

from core.authority.risk import known

_RAIZ = Path(__file__).resolve().parents[2]
_NOME_RE = re.compile(r'ToolSchema\(\s*name="([a-z_0-9]+)"', re.MULTILINE)


def _nomes_declarados() -> set[str]:
    nomes: set[str] = set()
    for pasta in ("core", "modules"):
        for arquivo in (_RAIZ / pasta).rglob("tools.py"):
            if "__pycache__" in str(arquivo):
                continue
            nomes |= set(_NOME_RE.findall(arquivo.read_text(encoding="utf-8")))
    return nomes


def test_a_varredura_acha_o_conjunto_de_tools():
    """Se o regex parar de casar, o teste abaixo passa por vazio — trava isso."""
    nomes = _nomes_declarados()
    assert len(nomes) > 60, f"varredura achou só {len(nomes)} tools — regex quebrou?"
    assert {"email_send", "neo_bash", "web_fetch", "search_pks"} <= nomes


def test_toda_tool_declarada_tem_classificacao():
    sem_classe = sorted(n for n in _nomes_declarados() if not known(n))
    assert not sem_classe, (
        "tools sem classificação em core/authority/risk.py: "
        + ", ".join(sem_classe)
    )
