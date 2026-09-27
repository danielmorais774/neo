"""O cérebro nunca recebe uma primitiva de transporte do navegador.

docs/browser-surface-plan.md §13.1. Mover a biblioteca de `js_*` para o core cria
uma tentação concreta: expor `browser.eval` como tool e deixar o modelo montar o
próprio JavaScript. Isso seria uma escotilha por fora de TODA a classificação —
a tabela de risco (`core/authority/risk.py`) e o portão de liberação raciocinam
sobre nomes de tool, e `eval` não tem nome de risco: tem todos.

A forma certa é a que o preenchimento escopado já usa (`release-request-plan`
§7.2): o modelo escreve uma intenção (`«pks:cep»`, `browser.click` com uma
referência), e quem tem o poder cru é o executor. Este teste é a trava disso.

Varre o FONTE, como o `test_risk_cobre_o_registry`, porque metade das tools entra
condicionalmente — o conjunto inteiro só existe depois do wiring.
"""

from __future__ import annotations

import re
from pathlib import Path

_RAIZ = Path(__file__).resolve().parents[2]
_NOME_RE = re.compile(r'ToolSchema\(\s*name="([a-z_0-9.]+)"', re.MULTILINE)

#: Nomes que NÃO podem ser tool, em nenhuma circunstância. `eval` e `cdp` dão ao
#: modelo execução arbitrária no navegador logado do usuário; `browser_eval` e
#: variações entram aqui porque o próximo a tentar vai tentar com underscore.
PROIBIDOS = {
    "browser_eval",
    "browser_cdp",
    "eval",
    "cdp",
    "evaluate",
    "run_js",
    "execute_js",
    "browser_execute_js",
}


def _nomes_declarados() -> set[str]:
    nomes: set[str] = set()
    for pasta in ("core", "modules"):
        for arquivo in (_RAIZ / pasta).rglob("*.py"):
            if "__pycache__" in str(arquivo):
                continue
            nomes.update(_NOME_RE.findall(arquivo.read_text(encoding="utf-8")))
    return nomes


def test_nenhuma_primitiva_de_transporte_e_tool():
    declaradas = _nomes_declarados()
    assert declaradas, "a varredura não achou tool nenhuma — o regex quebrou"

    vazaram = sorted(declaradas & PROIBIDOS)
    assert not vazaram, (
        "estas primitivas de transporte estão declaradas como tool do cérebro: "
        + ", ".join(vazaram)
        + ".\nO modelo não escreve JavaScript (docs/browser-surface-plan.md §13.1): "
        "exponha uma ação do vocabulário `browser.*` e deixe o JS na biblioteca do "
        "core, onde a tabela de risco alcança."
    )


def test_o_vocabulario_do_navegador_nao_e_catalogo_de_tools():
    """`browser.click` e afins são ações de CANAL, não tools.

    Se um dia alguém expor a ação de canal como tool, o modelo passa a endereçar
    aba por `tab_id` e referência numérica — que é exatamente o modo de falha do
    `file_id` e do `entity_id` do Home Assistant: id opaco no schema, modelo
    inventando um.
    """
    declaradas = _nomes_declarados()
    com_ponto = sorted(n for n in declaradas if n.startswith("browser."))
    assert not com_ponto, (
        "ação de canal declarada como tool: " + ", ".join(com_ponto)
    )


def test_a_escada_nao_nasce_com_derivada_nao_testada():
    """Cada derivada precisa de par de testes (§13.7) — o dicionário não é despejo.

    Este teste não proíbe crescer: ele obriga a crescer com prova. Quando você
    adicionar uma entrada em `DERIVADAS`, adicione o par de testes e o nome dela
    aqui.
    """
    from core.browser_local.surfaces import DERIVADAS

    COM_TESTE: set[str] = set()  # nenhuma portada ainda
    sem_prova = sorted(set(DERIVADAS) - COM_TESTE)
    assert not sem_prova, (
        "derivadas sem o par de testes do §13.7: " + ", ".join(sem_prova)
    )
