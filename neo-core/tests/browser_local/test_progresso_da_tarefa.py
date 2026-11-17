"""Progresso da tarefa pelo contador da página (sessão dade900f, 2026-11-08).

O Daniel abriu o caderno durante e depois de três jobs do Portal e não achou o
que procurava: em que questão a tarefa estava, que alternativa ficou marcada e
quantas faltavam. O caderno é escrito pelo modelo, e progresso escrito pelo
modelo falhou dos dois jeitos nos 20 diários da VPS: o job f43e2f02 respondeu 36
perguntas e não anotou nada; o 50227807 escreveu "P1..P29 já anotadas" 23 vezes.

Quem monta o progresso agora é o código: o contador da página ("Questão 7 de
10") diz onde a tarefa está, e o device diz o que ficou marcado.
"""

from __future__ import annotations

import pytest

from core.browser_local.driver import BrowserLocalDriver
from core.browser_local.progresso import Progresso, ler_contador, ordenar_por_numero

from .test_driver import _FakeLLM, _FakeProvider, _snapshot

_ALTERNATIVAS = {
    1: "As asserções I e II são verdadeiras, e a II é uma justificativa correta da I.",
    2: "As asserções I e II são verdadeiras, mas a II não é uma justificativa correta da I.",
    3: "A asserção I é uma proposição verdadeira e a II é uma proposição falsa.",
    4: "A asserção I é uma proposição falsa e a II é uma proposição verdadeira.",
    5: "As asserções I e II são falsas.",
}


def _portal(questao: int, total: int = 10, marcada: int | None = None, salva: bool = False):
    """O snapshot como o do Portal real (diário 9c9b5bae): cada alternativa vem
    duas vezes — o `<label>` com o texto e o rádio sem texto — e o contador fica
    no resto da página, junto dos botões."""
    elementos = []
    for k, texto in _ALTERNATIVAS.items():
        elementos.append(
            {"ref": 2 * k, "tag": "label", "text": f"Alternativa {k}: {texto}", "checked": k == marcada}
        )
        elementos.append({"ref": 2 * k + 1, "tag": "label", "text": "", "checked": k == marcada})
    elementos.append({
        "ref": 20, "tag": "button", "type": "submit", "text": "Responder",
        # Desabilitado antes de marcar e depois de salvar — habilitado só entre os dois.
        "disabled": marcada is None or salva,
    })
    return _snapshot(
        url="https://portal.exemplo.edu.br/#!/questionario/380129",
        elements=elementos,
        tela=f"10 QUESTÕES\nENUNCIADO DA QUESTÃO {questao}",
        resto=f"Feedback:\nQuestão {questao} de {total} Responder Finalizar\n© 2026 FACULDADE",
    )


# ── o contador ──────────────────────────────────────────────────────────────


def test_contador_do_portal_e_do_12axes():
    assert ler_contador(_portal(7)) == ("questao", "Questão", 7, 10)
    axes = _snapshot(tela="Pergunta 14 de 36 39% concluído\nSou contra implantes artificiais")
    assert ler_contador(axes) == ("pergunta", "Pergunta", 14, 36)
    arquetipo = _snapshot(tela="Identificando seu arquétipo 2 de 6\nComo o poder deve ser organizado?")
    assert ler_contador(arquetipo)[2:] == (2, 6)


def test_sem_contador_nao_ha_progresso_inventado():
    assert ler_contador(_snapshot(tela="Passo 8 de no máximo 53")) is None
    assert ler_contador(_snapshot(tela="página de exemplo")) is None


def test_dois_numeros_da_mesma_serie_e_lista_nao_contador():
    lista = _snapshot(tela="Questão 1 de 10 — certa\nQuestão 2 de 10 — errada")
    assert ler_contador(lista) is None


# ── marcada × respondida ────────────────────────────────────────────────────


def test_portal_marcar_e_responder_e_respondida_marcar_so_nao_e():
    p = Progresso()
    p.ver(_portal(1))
    p.marcou(f"Alternativa 3: {_ALTERNATIVAS[3]}")
    p.ver(_portal(1, marcada=3))
    p.clicou("Responder")  # no Portal, salva e já pula para a próxima
    p.ver(_portal(2))
    p.marcou(f"Alternativa 2: {_ALTERNATIVAS[2]}")
    p.ver(_portal(2, marcada=2))

    serie = p.series["questao"]
    assert serie.itens[1].respondida and serie.itens[1].como == "marquei"
    assert not serie.itens[2].respondida, "marcar sem Responder não salva no Portal"
    linhas = p.linhas()
    assert linhas[0] == "Progresso — Questão: 1 de 10 respondidas, 1 marcada(s) sem envio confirmado"
    assert any(l.startswith("- Questão 1 ✓ respondida — marquei «Alternativa 3:") for l in linhas)
    assert any(l.startswith("- Questão 2 ◐ marcada, envio não confirmado") for l in linhas)
    assert linhas[-1] == "- Não vistas nesta tarefa: 3–10"


def test_pagina_que_avanca_sozinha_depois_da_escolha_conta_como_respondida():
    p = Progresso()
    p.ver(_snapshot(tela="Pergunta 1 de 36 0% concluído"))
    p.marcou("Discordo")
    p.ver(_snapshot(tela="Pergunta 2 de 36 2% concluído"))
    assert p.series["pergunta"].itens[1].respondida


def test_quem_so_confere_ve_a_resposta_salva_pelo_botao_desabilitado():
    # Job de extração/conferência: nada é clicado, a página já mostra marcada.
    p = Progresso()
    p.ver(_portal(3, marcada=3, salva=True))
    item = p.series["questao"].itens[3]
    assert item.marcada.startswith("Alternativa 3:") and item.como == "já estava"
    assert item.respondida


def test_marcada_que_ainda_nao_foi_salva_nao_vira_respondida_so_por_ser_vista():
    p = Progresso()
    p.ver(_portal(4, marcada=1, salva=False))
    assert not p.series["questao"].itens[4].respondida


def test_a_pagina_e_a_verdade_quando_mostra_outra_marcada():
    p = Progresso()
    p.ver(_portal(5))
    p.marcou(f"Alternativa 1: {_ALTERNATIVAS[1]}")
    p.ver(_portal(5, marcada=4))
    item = p.series["questao"].itens[5]
    assert item.marcada.startswith("Alternativa 4:") and item.como == "na página"


def test_placar_no_prompt_e_numeros_e_manda_nao_anotar():
    p = Progresso()
    for q in (1, 2, 3):
        p.ver(_portal(q))
        p.marcou(f"Alternativa {q}: {_ALTERNATIVAS[q]}")
        p.clicou("Responder")
    p.ver(_portal(4))
    texto = p.para_o_prompt()
    assert "NÃO anote" in texto
    assert "Respondidas: 1–3." in texto and "Abertas sem nada marcado: 4." in texto
    assert "Ainda não vistas nesta tarefa: 5–10." in texto
    assert "Você está na Questão 4." in texto
    assert p.resumo() == "Questão 4 de 10 · 3 respondida(s)"


# ── a ordem do caderno ──────────────────────────────────────────────────────


def test_caderno_na_ordem_das_questoes_e_nao_na_da_coleta():
    # A ordem real do job f7794968: a aba abriu na Questão 3.
    coleta = [
        "Q3 — Enunciado: 'A abordagem semiótica…'",
        "Q1 — Enunciado: 'A análise semiótica…'",
        "Q2 — Enunciado: 'A interpretação de mapas…'",
        "Q7 — Enunciado: 'O gráfico intitulado…'",
        "Questão 7 — a tela mostrava: O gráfico tem 3 linhas",
        "Q8 — Enunciado: 'Segundo a ABNT…'",
    ]
    assert [n[:3] for n in ordenar_por_numero(coleta)] == ["Q1 ", "Q2 ", "Q3 ", "Q7 ", "Que", "Q8 "]


def test_caderno_que_nao_e_de_questoes_fica_na_ordem():
    produtos = ["PRODUTO 4: R$ 10", "PRODUTO 1: R$ 20", "PRODUTO 2: R$ 30"]
    assert ordenar_por_numero(produtos) == produtos


# ── o que pode ir para o caderno ────────────────────────────────────────────


def _driver_sem_device():
    return BrowserLocalDriver(act=None, llm=_FakeLLM(_FakeProvider([])), model="fake")


@pytest.mark.asyncio
async def test_narracao_nao_vira_nota_mas_o_enunciado_com_devo_vira():
    driver = _driver_sem_device()
    await driver._anotar_junto({"anotar": [
        "Avançando para a Questão 8.",
        "A aba está na tela de resultados, portanto, devo recomeçar o quiz.",
        "Q5 — Enunciado: 'Eu devo responder às perguntas com sinceridade?' — Concordo",
    ]})
    assert driver.notes == [
        "Q5 — Enunciado: 'Eu devo responder às perguntas com sinceridade?' — Concordo"
    ]


def test_leitura_da_visao_fica_presa_a_questao_e_inteira():
    driver = _driver_sem_device()
    driver.progresso.ver(_portal(7))
    descricao = (
        "O gráfico tem 3 linhas: Brasil (preta, com quadrados), Pará (verde-claro, com "
        "triângulos) e Norte (amarelo-esverdeado, com círculos). A tendência geral é de "
        "crescimento até 2017, queda em 2019 e leve recuperação em 2020. Em 2017, o Pará "
        "atinge seu pico (~53), acima da média nacional e da região Norte. Em 2020, todas "
        "caem, mas o Pará ainda fica acima do Brasil e da região Norte."
    )
    driver._anotar_visao(descricao, "Descreva o gráfico da Questão 7")
    assert driver.notes[0].startswith("Questão 7 — a tela mostrava: O gráfico tem 3 linhas")
    assert driver.notes[0].endswith("acima do Brasil e da região Norte."), "era cortada em 400"


def test_visao_que_nao_enxergou_nao_vira_nota():
    driver = _driver_sem_device()
    driver._anotar_visao(
        "O gráfico não está visível na tela — só aparece o texto da questão e as alternativas.",
        "Qual linha fica mais alta no gráfico?",
    )
    assert driver.notes == []


# ── o driver inteiro, num Portal de mentira ─────────────────────────────────


@pytest.mark.asyncio
async def test_o_progresso_vai_ao_vivo_para_fora_e_no_topo_do_resumo():
    estado = {"q": 1, "marcada": {}, "salva": set()}

    async def act(action, args, timeout):
        q = estado["q"]
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return _portal(q, total=3, marcada=estado["marcada"].get(q), salva=q in estado["salva"])
        if action == "browser.click":
            ref = args.get("ref")
            if ref == 20:
                estado["salva"].add(q)
                estado["q"] = q + 1
                return {"ok": True, "label": "Responder", "saiu": True}
            k = ref // 2
            estado["marcada"][q] = k
            return {"ok": True, "label": f"Alternativa {k}: {_ALTERNATIVAS[k]}", "checked": True}
        return {"ok": True}

    publicados: list[list[str]] = []

    async def on_progresso(linhas, resumo):
        publicados.append(list(linhas))

    passos = [
        {"thought": "marcar", "action": "click", "args": {"ref": 6}},
        {"thought": "enviar", "action": "click", "args": {"ref": 20}},
        {"thought": "marcar", "action": "click", "args": {"ref": 4}},
        {"thought": "enviar", "action": "click", "args": {"ref": 20}},
        {"thought": "marcar", "action": "click", "args": {"ref": 10}},
        {"action": "done", "args": {"answer": "Respondi as questões."}},
    ]
    driver = BrowserLocalDriver(
        act=act, llm=_FakeLLM(_FakeProvider(passos)), model="fake", on_progresso=on_progresso
    )
    result = await driver.run("responda as 3 questões sem finalizar")

    assert result.status == "completed"
    assert publicados, "o caderno ao vivo nunca recebeu nada"
    # Uma publicação por MUDANÇA, na ordem: aberta, marcada, respondida e na seguinte…
    assert any("· agora na Questão 2" in pub[0] for pub in publicados)
    assert any("- Questão 1 ◐ marcada, envio não confirmado" in "\n".join(pub) for pub in publicados)
    assert len(publicados) == len({tuple(pub) for pub in publicados}), "publicou sem mudar"
    assert result.progresso[0] == "Progresso — Questão: 2 de 3 respondidas, 1 marcada(s) sem envio confirmado"
    assert result.summary.startswith(result.progresso[0])
    assert "- Questão 3 ◐ marcada, envio não confirmado — marquei «Alternativa 5:" in result.summary


def test_clicar_de_novo_na_alternativa_ja_salva_nao_desfaz_o_envio():
    # Job 9c9b5bae: depois da Q10 o Portal voltou à Q1, o modelo achou que era a
    # Q9 e clicou na alternativa que já estava salva. A Q1 continuou respondida.
    p = Progresso()
    p.ver(_portal(1))
    p.marcou(f"Alternativa 3: {_ALTERNATIVAS[3]}")
    p.clicou("Responder")
    p.ver(_portal(1, marcada=3, salva=True))
    p.marcou(f"Alternativa 3: {_ALTERNATIVAS[3]}")
    assert p.series["questao"].itens[1].respondida
    # Trocar de alternativa desfaz até a nova confirmação.
    p.marcou(f"Alternativa 5: {_ALTERNATIVAS[5]}")
    assert not p.series["questao"].itens[1].respondida


# ── página SEM contador: o total do pedido e a contagem do que foi confirmado ──
# (opção "c", escolhida pelo Daniel em 2026-11-08)

from core.browser_local.progresso import total_do_pedido  # noqa: E402


def _sem_numero(marcadas=(), url="https://quiz.exemplo/q", grupos=1):
    """Página sem "Questão N de M": um ou mais grupos de quatro opções."""
    elementos = []
    for g in range(grupos):
        for k in range(1, 5):
            ref = 10 * g + k
            elementos.append({
                "ref": ref, "tag": "label", "text": f"Grupo {g} opção {k}",
                "checked": ref in marcadas, "controla": "radio[resp]",
            })
    elementos.append({"ref": 99, "tag": "button", "text": "Responder"})
    return _snapshot(url=url, elements=elementos, tela="Escolha a melhor opção.")


def test_total_vem_do_pedido_escrito_como_o_daniel_escreve():
    assert total_do_pedido("responde essa atividade, sao 10 questoes e no final vc nao finaliza") == 10
    assert total_do_pedido("", "select 'Short 36 questions' and answer") == 36
    assert total_do_pedido("marque uma das 5 alternativas") is None
    # O do Daniel vem antes do que o cérebro escreveu.
    assert total_do_pedido("são 12 perguntas", "responda as 10 questões") == 12


def test_sem_contador_e_sem_total_no_pedido_nao_ha_bloco():
    p = Progresso()
    p.ver(_sem_numero())
    p.marcou("Grupo 0 opção 2", ref=2, url="https://quiz.exemplo/q")
    assert not p and p.linhas() == [] and p.para_o_prompt() == ""


def test_trocar_de_alternativa_na_mesma_questao_nao_conta_duas():
    p = Progresso()
    p.total_pedido = 10
    p.ver(_sem_numero())
    p.marcou("Grupo 0 opção 1", ref=1, url="https://quiz.exemplo/q")
    p.ver(_sem_numero(marcadas={1}))
    p.marcou("Grupo 0 opção 4", ref=4, url="https://quiz.exemplo/q")
    # O rádio desmarcou a anterior: mesmo grupo, mesma questão.
    p.ver(_sem_numero(marcadas={4}))
    assert [m.rotulo for m in p.marcacoes] == ["Grupo 0 opção 4"]
    assert p.resumo() == "1 de 10 questões marcadas (0 com envio confirmado)"


def test_formulario_de_pagina_unica_conta_cada_grupo():
    p = Progresso()
    p.total_pedido = 2
    p.ver(_sem_numero(grupos=2))
    p.marcou("Grupo 0 opção 2", ref=2, url="https://quiz.exemplo/q")
    p.ver(_sem_numero(marcadas={2}, grupos=2))
    p.marcou("Grupo 1 opção 3", ref=13, url="https://quiz.exemplo/q")
    # A primeira continua marcada: são grupos (questões) diferentes.
    p.ver(_sem_numero(marcadas={2, 13}, grupos=2))
    assert len(p.marcacoes) == 2


def test_envio_e_pagina_que_avanca_marcam_respondida_e_abrem_a_proxima():
    p = Progresso()
    p.total_pedido = 3
    p.ver(_sem_numero())
    p.marcou("Grupo 0 opção 1", ref=1, url="https://quiz.exemplo/q")
    p.ver(_sem_numero(marcadas={1}))
    p.clicou("Responder")
    # A questão seguinte reaproveita os MESMOS refs e rótulos.
    p.ver(_sem_numero())
    p.marcou("Grupo 0 opção 1", ref=1, url="https://quiz.exemplo/q")
    # Esta avança sozinha: a próxima tela vem com tudo desmarcado.
    p.ver(_sem_numero())
    p.marcou("Grupo 0 opção 3", ref=3, url="https://quiz.exemplo/q")
    p.ver(_sem_numero(marcadas={3}))

    assert [m.respondida for m in p.marcacoes] == [True, True, False]
    linhas = p.linhas()
    assert linhas[0].startswith("Progresso — 3 de 3 questões marcadas (2 com envio confirmado)")
    assert linhas[1] == "- 1ª marcação ✓ respondida — marquei «Grupo 0 opção 1»"
    assert linhas[3] == "- 3ª marcação ◐ marcada, envio não confirmado — marquei «Grupo 0 opção 3»"
    assert "o pedido fala em 3" in p.para_o_prompt()


def test_contador_na_pagina_tem_precedencia_sobre_a_contagem():
    p = Progresso()
    p.total_pedido = 10
    p.ver(_portal(1))
    p.marcou(f"Alternativa 3: {_ALTERNATIVAS[3]}", ref=6, url="https://portal")
    assert p.marcacoes == [] and p.series["questao"].itens[1].marcada


@pytest.mark.asyncio
async def test_driver_conta_pelo_pedido_quando_a_pagina_nao_numera():
    estado = {"marcada": set()}

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return _sem_numero(marcadas=estado["marcada"])
        if action == "browser.click":
            ref = args.get("ref")
            if ref == 99:
                estado["marcada"] = set()
                return {"ok": True, "label": "Responder"}
            estado["marcada"] = {ref}
            return {"ok": True, "label": f"Grupo 0 opção {ref}", "checked": True}
        return {"ok": True}

    passos = [
        {"action": "click", "args": {"ref": 2}},
        {"action": "click", "args": {"ref": 99}},
        {"action": "click", "args": {"ref": 4}},
        {"action": "done", "args": {"answer": "Respondi."}},
    ]
    driver = BrowserLocalDriver(
        act=act, llm=_FakeLLM(_FakeProvider(passos)), model="fake",
        pedido="responde essa atividade, sao 5 questoes",
    )
    result = await driver.run("responda as questões da atividade")
    assert result.progresso[0].startswith("Progresso — 2 de 5 questões marcadas (1 com envio confirmado)")
