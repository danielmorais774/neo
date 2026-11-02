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
