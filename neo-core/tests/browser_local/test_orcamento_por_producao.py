"""O orçamento de passos deixou de ser um número e virou uma régua.

O teto fixo de 25 tratava igual duas coisas opostas: a tarefa que está
entregando e ainda precisa de espaço, e a que está girando. Nos dois traces
reais ele errou dos dois lados:

* job `6633351e` anotou a resposta no passo 2 e gastou **23 passos e 110s**
  depois dela, terminando `failed` no teto — com a resposta certa no caderno;
* job `78d998ee` chegou ao `done` no passo 20 de 25, ou seja, quase não coube.

A régua é o **caderno**, não o movimento: ele é literalmente o que o job tem
para entregar. Rolar oito vezes para percorrer uma lista longa é trabalho (o
`78d998ee` fez isso e terminou certo); voltar e revoltar sem nada entrar no
caderno é giro.

Os limiares (`_SEM_GANHO_MAX = 12`) foram medidos contra esses dois traces, não
escolhidos: com 12 o job que terminou certo chega inteiro ao `done` e o que
girou para na metade; com 10 o job bom é amputado dois passos antes de fechar
sozinho.
"""

from __future__ import annotations

import json

import pytest

from core.browser_local import driver as driver_mod
from core.browser_local.driver import BrowserLocalDriver


class _Resposta:
    def __init__(self, content: str) -> None:
        self._content = content

    def get_content(self) -> str:
        return self._content


class _Provider:
    def __init__(self, steps: list[dict]) -> None:
        self.steps = list(steps)
        self.prompts: list[str] = []

    async def complete(self, **kwargs):
        self.prompts.append(kwargs["messages"][-1].content)
        passo = self.steps.pop(0) if self.steps else {"action": "scroll", "args": {"delta": 300}}
        return _Resposta(json.dumps(passo))


class _LLM:
    def __init__(self, provider) -> None:
        self.provider = provider

    def get_provider_for_model(self, model: str):
        return self.provider


def _tela(marker: str):
    return {
        "url": f"https://loja.exemplo/lista#{marker}",
        "title": f"Lista {marker}",
        "ready": "complete",
        "stop": False,
        "elements": [{"ref": 0, "tag": "a", "text": f"item {marker}"}],
        "text": f"conteúdo {marker}",
    }


def _driver(steps, *, telas=None, **kwargs):
    """``telas``: sequência de snapshots; a última se repete para sempre."""
    fila = list(telas) if telas else [_tela(f"t{i}") for i in range(200)]

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 7}
        if action == "browser.snapshot":
            return fila.pop(0) if len(fila) > 1 else fila[0]
        if action == "browser.extract":
            return {"ok": True, "text": ""}
        return {"ok": True}

    provider = _Provider(steps)
    driver = BrowserLocalDriver(
        act=act,
        llm=_LLM(provider),
        model="texto",
        vision_model=None,
        **kwargs,
    )
    driver._provider = provider  # type: ignore[attr-defined]
    return driver


def _rolagens(n: int) -> list[dict]:
    """Rolagens com delta DIFERENTE: a trava de repetição literal é outra
    guarda, e não é ela que está sob teste aqui."""
    return [{"action": "scroll", "args": {"delta": 200 + i * 10}} for i in range(n)]


@pytest.mark.asyncio
async def test_para_quando_o_caderno_para_de_crescer():
    """O desfecho do job 6633351e: resposta anotada cedo, e depois nada.

    Não é o teto que tem que pegar isso — quando ele pega, já se gastaram 23
    passos do navegador do usuário para entregar o que já estava pronto.
    """
    driver = _driver(
        [{"action": "note", "args": {"texto": "Produto Exemplo — chega segunda — R$ 49,86"}}]
        + _rolagens(40)
    )

    result = await driver.run("o que está a caminho?")

    assert result.status == "failed"
    assert "sem acrescentar nada" in result.summary
    assert driver._acting_steps <= driver_mod._SEM_GANHO_MAX + 1, (
        f"gastou {driver._acting_steps} passos depois da última anotação"
    )
    # O ponto todo de parar cedo: o que ele TEM chega junto.
    assert "Produto Exemplo" in result.summary


@pytest.mark.asyncio
async def test_procurar_antes_de_ter_algo_nao_e_improdutividade():
    """Com o caderno vazio, PROCURAR é o trabalho.

    Se a régua valesse desde o primeiro passo, toda tarefa que leva dez passos
    para achar a página morreria no caminho — que é o oposto do que isto existe
    para fazer.
    """
    driver = _driver(_rolagens(40), max_steps=20)

    result = await driver.run("acha a página de pedidos")

    assert "sem acrescentar nada" not in result.summary
    assert driver._acting_steps == 20, "tinha que ir até o teto, não parar antes"


@pytest.mark.asyncio
async def test_giro_com_caderno_vazio_para():
    """O outro desfecho: voltar à mesma tela sem nada para entregar.

    Duas telas se alternando é o padrão clássico — abre, não serve, volta,
    abre de novo.
    """
    a, b = _tela("A"), _tela("B")
    driver = _driver(
        [{"action": "click", "args": {"ref": 0}} for _ in range(20)],
        telas=[a, b, a, b, a, b, a, b],
    )

    result = await driver.run("abre os detalhes")

    assert result.status == "failed"
    assert "giro, não progresso" in result.summary
    assert driver._acting_steps < 8, f"levou {driver._acting_steps} passos para notar o giro"


@pytest.mark.asyncio
async def test_producao_devolve_passo_ao_orcamento():
    """O outro lado do pedido: teto fixo também mata tarefa que ESTÁ entregando.

    Coleta de 20 itens não cabe em 25 passos se cada item custa um clique e uma
    anotação. Cada crescimento do caderno devolve um passo, até o dobro do
    orçamento base — que continua sendo o limite duro, porque é o navegador
    dele e ele está olhando.
    """
    passos: list[dict] = []
    for i in range(30):
        passos.append({"action": "scroll", "args": {"delta": 200 + i}})
        passos.append({"action": "note", "args": {"texto": f"produto {i} — R$ {i}0,00"}})
    driver = _driver(passos, max_steps=10)

    await driver.run("colete os produtos")

    assert driver._teto_de_passos() > 10, "produzir não devolveu passo nenhum"
    assert driver._teto_de_passos() <= 20, "o teto duro é o dobro do orçamento base"
    assert driver._acting_steps > 10, "o orçamento elástico não foi usado"


@pytest.mark.asyncio
async def test_a_cutucada_vem_antes_da_parada():
    """A parada é a REDE; quem deve resolver é o aviso.

    Nos dois traces reais este aviso cai quatro passos antes de a parada
    precisar disparar — tempo de o modelo fechar sozinho com o que já tem, que
    é sempre melhor que ser cortado.
    """
    driver = _driver(
        [{"action": "note", "args": {"texto": "achei o que foi pedido"}}] + _rolagens(40)
    )

    await driver.run("o que está a caminho?")

    avisados = [p for p in driver._provider.prompts if "responda `done`" in p]
    assert avisados, "o modelo nunca foi avisado de que estava girando"
    assert len(avisados) >= driver_mod._SEM_GANHO_MAX - driver_mod._SEM_GANHO_AVISO


# ── os dois traces reais, como roteiro ──────────────────────────────────────
#
# A sequência exata de decisões de cada job, extraída do `trace.md`. É o que
# transforma "escolhi 12" em "12 é o número que faz estes dois acabarem certo".

#: job 6633351e — anotou a resposta na 2ª decisão e girou até o teto (`failed`).
ROTEIRO_QUE_GIROU = (
    "navigate note scroll scroll scroll scroll scroll click wait_for click "
    "wait_for click wait_for look press navigate scroll scroll extract scroll "
    "click extract extract press done click extract extract scroll"
).split()

#: job 78d998ee — anotou na 10ª decisão e fechou com `done` na 25ª. Terminou
#: CERTO: é o que não pode ser cortado.
ROTEIRO_QUE_DEU_CERTO = (
    "navigate click wait_for scroll scroll scroll scroll look scroll note "
    "scroll scroll scroll scroll scroll look scroll scroll scroll click look "
    "click look scroll done"
).split()


def _roteiro(acoes: list[str]) -> list[dict]:
    """Traduz a lista de ações do trace em decisões, com args plausíveis.

    Os `delta` variam de propósito: a trava de repetição literal é outra guarda
    e não é ela que está sob teste aqui.
    """
    passos: list[dict] = []
    for i, acao in enumerate(acoes):
        if acao == "note":
            passos.append({"action": "note", "args": {"texto": f"achado {i}: Produto Exemplo R$ 49,86"}})
        elif acao == "scroll":
            passos.append({"action": "scroll", "args": {"delta": 200 + i * 7}})
        elif acao == "navigate":
            passos.append({"action": "navigate", "args": {"url": f"https://loja.exemplo/p{i}"}})
        elif acao == "done":
            passos.append({"action": "done", "args": {"answer": "achei o que você pediu"}})
        elif acao == "wait_for":
            passos.append({"action": "wait_for", "args": {"text": f"marca {i}"}})
        elif acao == "look":
            passos.append({"action": "look", "args": {"pergunta": f"o que tem aqui {i}?"}})
        elif acao == "press":
            passos.append({"action": "press", "args": {"key": "Escape"}})
        elif acao == "extract":
            passos.append({"action": "extract", "args": {"ref": i}})
        else:
            passos.append({"action": "click", "args": {"ref": i}})
    return passos


@pytest.mark.asyncio
async def test_o_job_que_girou_para_na_metade():
    """`6633351e`: 27 passos de atuação, `failed` no teto, com a resposta
    anotada desde a segunda decisão."""
    driver = _driver(_roteiro(ROTEIRO_QUE_GIROU), max_steps=25)

    result = await driver.run("o que está a caminho?")

    assert "sem acrescentar nada" in result.summary
    assert driver._acting_steps < 20, (
        f"parou em {driver._acting_steps} — o job real gastou 27"
    )
    assert "Produto Exemplo" in result.summary, "o caderno tem que chegar junto"


@pytest.mark.asyncio
async def test_o_job_que_deu_certo_continua_chegando_ao_done():
    """`78d998ee`: terminou CERTO no passo 20 de 25.

    É o contrapeso do teste acima, e é ele que define o limiar: uma régua que
    corta este job troca um desperdício por uma tarefa perdida.
    """
    driver = _driver(_roteiro(ROTEIRO_QUE_DEU_CERTO), max_steps=25)

    result = await driver.run("o que está a caminho?")

    assert result.status == "completed", f"foi cortado: {result.summary[:120]}"
    assert "achei o que você pediu" in result.summary


# ── nota ATUALIZADA que cresce também é produção, com três provas ───────────
#
# Job 379df104 (2026-11-05): quiz de 36 perguntas, respostas numa nota só que
# cresce a cada clique. A régua só via nota NOVA e parou o job na 15ª. As três
# provas (nota ganhou palavras, interação que deu certo, palavras inéditas na
# tela) vêm do replay dos 20 diários da VPS — cada teste abaixo é um dos casos
# que a régua não pode soltar.

_TEMAS = [
    "orçamento público equilibrado sempre prioritário",
    "pena capital punindo homicidas cruéis",
    "fronteiras abertas imigrantes refugiados acolhidos",
    "usinas nucleares geração elétrica limpa",
    "privatizar correios empresas estatais deficitárias",
    "renda universal paga mensalmente cidadãos",
    "alistamento militar obrigatório jovens rapazes",
    "tarifas protecionistas siderurgia nacional aço",
    "homeschooling crianças longe escolas",
    "voto facultativo eleitores ausentes urnas",
    "cannabis medicinal farmácias receitas médicas",
    "autonomia monetária juros inflação controlada",
    "reforma agrária latifúndios improdutivos desapropriados",
    "tarifa zero metrô trens ônibus",
    "regulação plataformas digitais moderação conteúdo",
    "interrupção gestação legalizada hospitais",
    "porte armas civis treinados",
    "sindicalismo greves trabalhadores fábricas",
    "subsídios veículos elétricos baterias",
    "ensino religioso confessional colégios",
]

#: O que a página faz quando o modelo age nela. Snapshot, banner e afins não
#: mexem na tela; é isso que deixa cada passo ver a tela que a ação produziu.
_ACOES_NA_PAGINA = {"browser.click", "browser.wait_for", "browser.scroll"}


def _driver_que_avanca(steps, telas, *, marca: bool = False, **kwargs):
    """Como `_driver`, mas a tela só avança quando uma ação mexe na página.

    ``marca``: o clique volta como o device devolve o de um rádio, com o
    controle lido MARCADO depois do clique (o "→ SELECIONADO").
    """
    pos = {"i": 0}

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 7}
        if action == "browser.snapshot":
            return telas[min(pos["i"], len(telas) - 1)]
        if action in _ACOES_NA_PAGINA:
            pos["i"] += 1
        if action == "browser.click" and marca:
            return {"ok": True, "checked": True}
        if action == "browser.extract":
            return {"ok": True, "text": ""}
        return {"ok": True}

    provider = _Provider(steps)
    driver = BrowserLocalDriver(act=act, llm=_LLM(provider), model="texto", vision_model=None, **kwargs)
    driver._provider = provider  # type: ignore[attr-defined]
    return driver


def _pergunta(i: int, tema: str) -> dict:
    return {
        "url": "https://quiz.exemplo/pt",
        "title": "Quiz",
        "ready": "complete",
        "stop": False,
        "elements": [
            {"ref": 0, "tag": "button", "type": "button", "text": "Concordo"},
            {"ref": 1, "tag": "h2", "text": f"Pergunta {i} de 20"},
        ],
        "text": f"Pergunta {i} de 20",
        "tela": f"Pergunta {i} de 20 — Você apoia {tema}? Concordo Discordo Neutro",
    }


def _respostas(ate: int) -> str:
    return "Respostas do quiz: " + " ".join(
        f"Q{i + 1} ({_TEMAS[i]}): Concordo." for i in range(ate)
    )


@pytest.mark.asyncio
async def test_quiz_com_respostas_numa_nota_so_vai_ate_o_fim():
    n = len(_TEMAS)
    passos = [
        {"action": "click", "args": {"ref": 0}, "anotar": _respostas(i + 1)} for i in range(n)
    ] + [{"action": "done", "args": {"answer": "respondi as 20"}}]
    telas = [_pergunta(i + 1, tema) for i, tema in enumerate(_TEMAS)]
    telas.append({**_pergunta(21, "fim"), "tela": "Resultado final do seu perfil ideológico"})
    driver = _driver_que_avanca(passos, telas, max_steps=12)

    result = await driver.run("responda o quiz")

    assert "sem acrescentar nada" not in result.summary
    assert result.status == "completed", f"foi cortado: {result.summary[:160]}"
    assert len(driver.notes) == 1, "as respostas tinham que continuar numa nota só"


#: Palavras de narração, todas distintas: cada passo do "carregando" faz a nota
#: crescer >= 3 palavras, como o "ainda carregando" do b8dbde34.
_NARRACAO = (
    "carregando lento girando spinner esperando aguardando conteúdo ainda vazio "
    "branco travado demorando servidor resposta pendente tela cinza ícone rodando "
    "sem questões visíveis aparecendo nada novo mostrado página parada mesma coisa "
    "continua igual sempre infinito eterno barra progresso parcial meio caminho "
    "quase pronto talvez breve logo instantes momento segundo minuto paciência "
    "espera fila processando sistema ocupado sobrecarga lentidão atraso enorme "
    "grande demais longo bastante tempo perdido gasto inútil vão esforço tentativa "
    "repetida outra mais uma vez novamente insistindo persistindo teimando seguindo "
    "olhando vendo conferindo checando revisando acompanhando observando notando "
    "registrando escrevendo anotando contando somando juntando reunindo coletando"
).split()


@pytest.mark.asyncio
async def test_carregando_com_relogio_e_nota_narrada_para():
    """Job b8dbde34: `wait_for` em "Carregando…", a nota ganhando "ainda carregando"
    e a página mudando a cada passo só no relógio. É giro, e tem que parar."""
    passos = [{"action": "note", "args": {"texto": "Atividade de Estudo, prazo 15/11, aberta"}}]
    passos += [
        {
            "action": "wait_for",
            "args": {"text": f"Questão {i}"},
            # Sem número que mude: número diferente faz `_mesma_nota` abrir nota
            # NOVA, e nota nova já era produção antes desta régua.
            "anotar": "Atividade de Estudo, prazo 15/11, aberta. Página ainda exibindo "
            + " ".join(_NARRACAO[: 3 * (i + 1)]),
        }
        for i in range(30)
    ]
    telas = [
        {
            **_tela("c"),
            "elements": [{"ref": 0, "tag": "span", "text": f"13:46:{i:02d}"}],
            "tela": f"Carregando Questionário... 13:46:{i:02d}",
        }
        for i in range(40)
    ]
    driver = _driver_que_avanca(passos, telas, max_steps=40)

    result = await driver.run("responda o questionário")

    assert "sem acrescentar nada" in result.summary
    assert driver._acting_steps <= driver_mod._SEM_GANHO_MAX + 1


@pytest.mark.asyncio
async def test_mesma_pergunta_de_novo_nao_rende_passo():
    """Job 21c563c1: a mesma questão respondida em volta — clique certo, nota
    crescendo, a seleção mudando a página, mas nenhuma palavra que a tela já não
    tivesse mostrado."""
    base = _pergunta(10, "energia nuclear usinas")
    telas = [_pergunta(9, "privatização correios")] + [
        {**base, "elements": [{**base["elements"][0], "checked": i % 2 == 0}, base["elements"][1]]}
        for i in range(40)
    ]
    passos = [{"action": "click", "args": {"ref": 0}, "anotar": _respostas(i + 1)} for i in range(20)]
    driver = _driver_que_avanca(passos, telas, max_steps=40)

    result = await driver.run("responda o quiz")

    assert "sem acrescentar nada" in result.summary
    assert driver._acting_steps <= driver_mod._SEM_GANHO_MAX + 2


@pytest.mark.asyncio
async def test_rolar_lista_atualizando_a_nota_continua_como_era():
    """Rolagem não é interação: a régua de antes (nota NOVA) segue valendo para ela."""
    passos = [
        {"action": "scroll", "args": {"delta": 200 + i}, "anotar": _respostas(i + 1)}
        for i in range(20)
    ]
    telas = [_pergunta(i + 1, tema) for i, tema in enumerate(_TEMAS)]
    driver = _driver_que_avanca(passos, telas, max_steps=40)

    result = await driver.run("leia a lista")

    assert "sem acrescentar nada" in result.summary


# ── sem nota nenhuma: a opção marcada e a pergunta nova (job 90b58ca3) ──────


@pytest.mark.asyncio
async def test_quiz_respondido_sem_anotar_vai_ate_o_fim():
    """O mesmo quiz, uma hora depois: perguntas 20 a 31 respondidas sem uma nota.
    A única nota do job é a do começo — e é ela que arma a régua."""
    passos = [{"action": "note", "args": {"texto": "Quiz 12axes: modo curto, 36 perguntas"}}]
    passos += [{"action": "click", "args": {"ref": 0}} for _ in _TEMAS]
    passos += [{"action": "done", "args": {"answer": "respondi as 20"}}]
    telas = [_pergunta(i + 1, tema) for i, tema in enumerate(_TEMAS)]
    telas.append({**_pergunta(21, "fim"), "tela": "Resultado final do seu perfil ideológico"})
    driver = _driver_que_avanca(passos, telas, marca=True, max_steps=12)

    result = await driver.run("responda o quiz")

    assert "sem acrescentar nada" not in result.summary
    assert result.status == "completed", f"foi cortado: {result.summary[:160]}"


@pytest.mark.asyncio
async def test_mesma_pergunta_marcada_de_novo_nao_rende_passo():
    """Marcar de novo a opção da MESMA pergunta não traz palavra nova (21c563c1)."""
    base = _pergunta(10, "energia nuclear usinas")
    telas = [_pergunta(9, "privatização correios")] + [
        {**base, "elements": [{**base["elements"][0], "checked": i % 2 == 0}, base["elements"][1]]}
        for i in range(40)
    ]
    passos = [{"action": "note", "args": {"texto": "Quiz: pergunta 9 respondida"}}]
    passos += [{"action": "click", "args": {"ref": 0}} for _ in range(20)]
    driver = _driver_que_avanca(passos, telas, marca=True, max_steps=40)

    result = await driver.run("responda o quiz")

    assert "sem acrescentar nada" in result.summary
    assert driver._acting_steps <= driver_mod._SEM_GANHO_MAX + 2


@pytest.mark.asyncio
async def test_clique_que_nao_marca_nada_segue_a_regua_de_antes():
    """Clique em link leva a páginas novas sem ser resposta (o giro do 6633351e
    depois da resposta anotada): sem controle marcado, nada muda."""
    passos = [{"action": "note", "args": {"texto": "Produto Exemplo — chega segunda — R$ 49,86"}}]
    passos += [{"action": "click", "args": {"ref": 0}} for _ in range(30)]
    telas = [_pergunta(i + 1, tema) for i, tema in enumerate(_TEMAS)] * 2
    driver = _driver_que_avanca(passos, telas, max_steps=40)

    result = await driver.run("o que está a caminho?")

    assert "sem acrescentar nada" in result.summary
    assert driver._acting_steps <= driver_mod._SEM_GANHO_MAX + 2
