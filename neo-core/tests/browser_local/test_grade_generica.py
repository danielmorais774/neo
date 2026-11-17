"""A grade em qualquer site, não só no Google Forms em português (2026-11-14).

O Daniel: "o juiz parece que só foi feito para esse formulário, e o Neo é para ser
para todos os sites". Medido antes do conserto, com o leitor de então:

- Google Forms em português → 1 pergunta, certa;
- o mesmo em inglês → as colunas vinham "Conforme, response for Clareza";
- matriz em <table> de rádios comuns (SurveyMonkey, Moodle) → 0 perguntas.

O snapshot passou a mandar em cada rádio, em `grade`, a estrutura que ele tem na
página (grupo, linha, coluna, pergunta). Os snapshots abaixo são os campos que o
Chromium de verdade produziu no teste `o_radio_de_grade_traz_grupo_linha_coluna_e_pergunta`
do desktop.
"""

from __future__ import annotations

from core.browser_local.grade import ler_grades

COLS = ["Conforme", "Não Conforme", "Parcialmente", "Não Aplicável"]


def _google_antigo(formato: str) -> dict:
    """Snapshot SEM os campos novos (desktop antigo): só a ordem do Google."""
    els = [{"ref": 1, "tag": "div", "text": "RF-01: O sistema deve permitir sair. *"}]
    ref = 2
    for linha in ("Clareza", "Completude"):
        els.append({"ref": ref, "tag": "div", "type": "radiogroup", "text": linha})
        ref += 1
        for coluna in COLS:
            els.append({"ref": ref, "tag": "div", "type": "radio", "text": formato.format(c=coluna, l=linha)})
            ref += 1
    return {"elements": els}


def test_google_em_ingles_tem_as_mesmas_colunas_que_em_portugues():
    pt = ler_grades(_google_antigo("{c}, resposta para {l}"))
    en = ler_grades(_google_antigo("{c}, response for {l}"))
    es = ler_grades(_google_antigo("{c}, respuesta para {l}"))
    for qs in (pt, en, es):
        [q] = qs
        assert q.chave == "RF-01" and q.colunas == COLS
        assert [l.rotulo for l in q.linhas] == ["Clareza", "Completude"]


# O que o Chromium devolveu (só os campos que importam aqui).
DO_CHROMIUM = {"elements": [
    {"ref": 0, "tag": "div", "type": "radio", "text": "Conforme, response for Clareza", "checked": False,
     "grade": {"grupo": "g1", "linha": "Clareza", "pergunta": "RF-01: O sistema deve permitir sair. *"}},
    {"ref": 1, "tag": "div", "type": "radio", "text": "Parcialmente, response for Clareza", "checked": False,
     "grade": {"grupo": "g1", "linha": "Clareza", "pergunta": "RF-01: O sistema deve permitir sair. *"}},
    {"ref": 2, "tag": "div", "type": "radio", "text": "Conforme, response for Completude", "checked": False,
     "grade": {"grupo": "g2", "linha": "Completude", "pergunta": "RF-01: O sistema deve permitir sair. *"}},
    {"ref": 3, "tag": "div", "type": "radio", "text": "Parcialmente, response for Completude", "checked": True,
     "grade": {"grupo": "g2", "linha": "Completude", "pergunta": "RF-01: O sistema deve permitir sair. *"}},
    {"ref": 4, "tag": "div", "type": "radio", "text": "Superior", "checked": False,
     "grade": {"grupo": "g3", "linha": "Qual a sua escolaridade? *", "pergunta": "Qual a sua escolaridade? *"}},
    {"ref": 5, "tag": "div", "type": "radio", "text": "Mestrado", "checked": False,
     "grade": {"grupo": "g3", "linha": "Qual a sua escolaridade? *", "pergunta": "Qual a sua escolaridade? *"}},
    {"ref": 6, "tag": "input", "type": "radio", "text": "1", "checked": False,
     "grade": {"grupo": "n:rf02_0", "linha": "Clareza", "coluna": "Conforme",
               "pergunta": "RF-02: O sistema deve exibir o perfil."}},
    {"ref": 7, "tag": "input", "type": "radio", "text": "2", "checked": True,
     "grade": {"grupo": "n:rf02_0", "linha": "Clareza", "coluna": "Parcialmente",
               "pergunta": "RF-02: O sistema deve exibir o perfil."}},
    {"ref": 8, "tag": "input", "type": "radio", "text": "1", "checked": False,
     "grade": {"grupo": "n:rf02_1", "linha": "Completude", "coluna": "Conforme",
               "pergunta": "RF-02: O sistema deve exibir o perfil."}},
    {"ref": 9, "tag": "input", "type": "radio", "text": "2", "checked": False,
     "grade": {"grupo": "n:rf02_1", "linha": "Completude", "coluna": "Parcialmente",
               "pergunta": "RF-02: O sistema deve exibir o perfil."}},
    {"ref": 10, "tag": "label", "text": "Azul", "checked": False,
     "grade": {"grupo": "n:cor", "pergunta": "Cor favorita"}},
    {"ref": 11, "tag": "input", "type": "radio", "text": "on", "checked": False,
     "grade": {"grupo": "n:cor", "pergunta": "Cor favorita"}},
    # Linha de tabela: `grupo` SOLTO é "caixa com N itens", outra coisa.
    {"ref": 14, "tag": "tr", "text": "Clareza", "grupo": 2},
    {"ref": 15, "tag": "tr", "text": "Completude", "grupo": 2},
]}


def test_aria_em_ingles_e_tabela_html_viram_a_mesma_estrutura():
    rf01, rf02 = ler_grades(DO_CHROMIUM)
    assert rf01.chave == "RF-01" and rf01.rotulo == "RF-01: O sistema deve permitir sair."
    assert [(l.rotulo, l.marcada) for l in rf01.linhas] == [("Clareza", ""), ("Completude", "Parcialmente")]
    assert rf01.linha("Clareza").radios == {"Conforme": 0, "Parcialmente": 1}
    assert rf02.chave == "RF-02"
    assert rf02.colunas == ["Conforme", "Parcialmente"], "o cabeçalho da coluna, não o value do rádio"
    assert [(l.rotulo, l.marcada) for l in rf02.linhas] == [("Clareza", "Parcialmente"), ("Completude", "")]


def test_escolha_simples_nao_e_grade():
    """O grupo É a pergunta (Google) ou não tem nome de linha (fieldset): fica de fora.

    Julgar escolha simples precisa da guarda que separa avaliar de responder
    sobre o Daniel em todas as perguntas — é a etapa 2.
    """
    chaves = [q.chave for q in ler_grades(DO_CHROMIUM)]
    assert chaves == ["RF-01", "RF-02"]


def test_escolha_simples_do_google_antigo_continua_fora():
    """Página 1 real (sessão 8e17b81e): o `label` entre os rádios encerra a linha."""
    els = [
        {"ref": 12, "tag": "div", "type": "radiogroup", "text": "Declaro que sou maior de idade *"},
        {"ref": 13, "tag": "label", "text": "Sim"},
        {"ref": 14, "tag": "div", "type": "radio", "text": "Sim"},
        {"ref": 15, "tag": "label", "text": "Não"},
        {"ref": 16, "tag": "div", "type": "radio", "text": "Não"},
    ]
    assert ler_grades({"elements": els}) == []


def test_linha_sem_nome_acha_o_radiogroup_do_inventario():
    els = [{"ref": 1, "tag": "div", "type": "radiogroup", "text": "Clareza"}]
    for i, coluna in enumerate(COLS[:2]):
        els.append({
            "ref": 2 + i, "tag": "div", "type": "radio", "text": f"{coluna}, response for Clareza",
            "grade": {"grupo": "g1", "pergunta": "RF-09: Exportar relatório."},
        })
    [q] = ler_grades({"elements": els})
    assert [l.rotulo for l in q.linhas] == ["Clareza"]
    assert q.colunas == COLS[:2]


def test_o_radio_de_grade_nao_vira_caixa_no_prompt():
    """`grupo` solto quer dizer "caixa com N itens — não é botão". A estrutura vai aninhada."""
    from core.browser_local.driver import BrowserLocalDriver

    async def act(*_):
        return {"ok": True}

    driver = BrowserLocalDriver(act=act, llm=None, model="x")
    tela = driver._render({"url": "https://exemplo.com/f", "title": "Form", **DO_CHROMIUM})
    linha = next(l for l in tela.splitlines() if "Conforme, response for Clareza" in l)
    assert "CAIXA" not in linha


# ── a aba Modelos ─────────────────────────────────────────────────────────────


def test_os_selects_do_navegador_ficam_juntos_e_o_juiz_nao_fala_daquele_formulario():
    from modules.model_config.store import ROLES, build_catalog

    roles = build_catalog(["openrouter", "deepseek"], {})["roles"]
    grupos = [r["group"] for r in roles]
    assert all(grupos), "todo card tem seção"
    local = [r["key"] for r in roles if r["group"] == "Navegador local (o seu)"]
    assert local == ["browser_local", "browser_local_vision", "browser_local_judge"]
    # As seções não se intercalam: cada uma aparece num bloco só.
    blocos = [g for i, g in enumerate(grupos) if i == 0 or grupos[i - 1] != g]
    assert len(blocos) == len(set(blocos))
    juiz = ROLES["browser_local_judge"]
    assert "grade de avaliação" not in juiz.label and "requisitos" not in juiz.description
    assert "qualquer site" in juiz.description
