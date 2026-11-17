"""A etapa nova não é o site reiniciando (job f9370b43, 12axes, 2026-11-05).

O agente respondeu as 36 perguntas. A 36ª desmontou o cartão para a etapa
seguinte ("Identifying your archetype 1 of 6") entrar, e três coisas se somaram:

  * o resultado do clique disse "NÃO ficou selecionado", porque o device leu o
    `aria-checked` de um nó que já tinha saído da página;
  * a decisão seguinte, que estava CERTA (responder a etapa nova), foi recusada
    por aspas sem escape dentro do `thought`;
  * na nova rodada o modelo leu "1 of 6" como o quiz reiniciando e clicou em
    "Back to start", e nada parou o clique que apagou as 36 respostas.
"""

from __future__ import annotations

import pytest

from core.browser_local.driver import BrowserLocalDriver

from .test_alvos_invisiveis import _alternativa
from .test_driver import _FakeLLM, _FakeProvider, _snapshot


# ── o controle que sai da página no clique ──────────────────────────────────


@pytest.mark.asyncio
async def test_controle_que_saiu_da_pagina_diz_que_a_pagina_reagiu():
    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 1}
        if action == "browser.snapshot":
            return _snapshot(elements=[_alternativa(0, "Disagree")])
        if action == "browser.click":
            return {"ok": True, "label": "Disagree", "saiu": True}
        return {"ok": True}

    driver = BrowserLocalDriver(
        act=act,
        llm=_FakeLLM(
            _FakeProvider(
                [
                    {"action": "click", "args": {"ref": 0}},
                    {"action": "done", "args": {"answer": "respondi"}},
                ]
            )
        ),
        model="fake",
    )
    result = await driver.run("responda a pergunta")
    passo = next(s for s in result.steps if s.action == "click")
    assert "SAIU da tela" in (passo.detail or "")
    assert "NÃO ficou selecionado" not in (passo.detail or "")


# ── aspas sem escape no `thought` ───────────────────────────────────────────

#: A forma da resposta real do passo 39: código citado no meio do raciocínio.
_THOUGHT_COM_ASPAS = (
    '{"thought": "O passo anterior foi `click({"ref": 5})`, que levou a esta tela. '
    "A opção 'C Live and let live' parece a escolha.\", "
    '"anotar": ["Arquétipo 1/6: What should society rest on?"], '
    '"action": "click", "args": {"ref": 6}}'
)


def test_aspas_sem_escape_no_thought_nao_derrubam_a_decisao():
    decisao = BrowserLocalDriver._parse(_THOUGHT_COM_ASPAS, cortado=False)
    assert decisao["action"] == "click"
    assert decisao["args"] == {"ref": 6}
    assert decisao["anotar"] == ["Arquétipo 1/6: What should society rest on?"]
    assert 'click({"ref": 5})' in decisao["thought"]


def test_raciocinio_que_cita_a_chave_action_tambem_e_consertado():
    raw = '{"thought": "o exemplo era {"action": "done"} e não serve", "action": "click", "args": {"ref": 2}}'
    assert BrowserLocalDriver._parse(raw, cortado=False)["args"] == {"ref": 2}


@pytest.mark.parametrize(
    "raw",
    [
        # o conserto mexe só no `thought`: argumento quebrado continua recusado
        '{"thought": "x "y" z", "action": "click", "args": {"ref": }}',
        '{"action": "done", "answer": pendrive}',
        '{"thought": "pronto", "action": "done", "args": {"answer": "ok"}',
    ],
)
def test_conserto_nao_inventa_acao_nem_argumento(raw):
    with pytest.raises(ValueError) as erro:
        BrowserLocalDriver._parse(raw, cortado=False)
    assert "não entendi a resposta do modelo" in str(erro.value)


def test_resposta_cortada_continua_sendo_corte():
    with pytest.raises(ValueError) as erro:
        BrowserLocalDriver._parse('{"thought": "a "b"", "action": "click", "args": {"re', cortado=True)
    assert "CORTADA por tamanho" in str(erro.value)


# ── o recomeço que joga fora o progresso ────────────────────────────────────


def _tela_da_etapa_nova(**over):
    return _snapshot(
        elements=[
            {"ref": 1, "tag": "a", "text": "Back to start", "chrome": True},
            {"ref": 6, "tag": "button", "text": "C Live and let live"},
        ],
        tela="Identifying your archetype 1 of 6\n01\nSociety\nWhat should society rest on?",
        **over,
    )


def _driver_com_caderno(n: int) -> BrowserLocalDriver:
    driver = BrowserLocalDriver(act=None, llm=_FakeLLM(_FakeProvider([])), model="fake")
    driver.notes = [f"Q{i}: resposta" for i in range(1, n + 1)]
    return driver


def test_recomecar_com_progresso_e_avisado_uma_vez_e_insistir_passa():
    driver = _driver_com_caderno(36)
    clique = {"action": "click", "args": {"ref": 1}}
    with pytest.raises(ValueError) as erro:
        driver._validate(clique, _tela_da_etapa_nova())
    msg = str(erro.value)
    assert "JOGA FORA" in msg and "36 anotações" in msg
    assert "Identifying your archetype 1 of 6" in msg, "mostra a tela que ele leu mal"
    assert "ETAPA NOVA" in msg
    # a mesma ação de novo é decisão, não descuido
    driver._validate(clique, _tela_da_etapa_nova())


def test_click_text_de_recomeco_tambem_passa_pela_guarda():
    driver = _driver_com_caderno(10)
    with pytest.raises(ValueError):
        driver._validate({"action": "click_text", "args": {"texto": "Restart quiz"}}, _tela_da_etapa_nova())


def test_sem_progresso_recomecar_nao_e_avisado():
    driver = _driver_com_caderno(2)
    driver._validate({"action": "click", "args": {"ref": 1}}, _tela_da_etapa_nova())


def test_clique_comum_com_progresso_nao_e_avisado():
    driver = _driver_com_caderno(36)
    driver._validate({"action": "click", "args": {"ref": 6}}, _tela_da_etapa_nova())


def test_o_aviso_rearma_quando_ha_progresso_novo_a_perder():
    driver = _driver_com_caderno(6)
    clique = {"action": "click", "args": {"ref": 1}}
    with pytest.raises(ValueError):
        driver._validate(clique, _tela_da_etapa_nova())
    driver._validate(clique, _tela_da_etapa_nova())
    driver.notes += [f"etapa nova {i}" for i in range(5)]
    with pytest.raises(ValueError):
        driver._validate(clique, _tela_da_etapa_nova())


@pytest.mark.parametrize(
    "rotulo,desfaz",
    [
        ("Back to start", True),
        ("Restart quiz", True),
        ("Recomeçar", True),
        ("Reiniciar teste", True),
        ("Start over", True),
        ("Limpar formulário", True),
        ("Começar de novo", True),
        ("Start short version", False),
        ("Next", False),
        ("Iniciar sessão", False),
        ("Restaurante Bom Prato", False),
        ("Startup", False),
    ],
)
def test_quais_rotulos_desfazem_o_progresso(rotulo, desfaz):
    from core.browser_local.driver import _DESFAZ_PROGRESSO

    assert bool(_DESFAZ_PROGRESSO.search(rotulo)) is desfaz
