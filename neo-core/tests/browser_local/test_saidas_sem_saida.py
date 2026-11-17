"""Três jobs mortos em 73 segundos (sessão 277a4afd, 2026-10-01).

O usuário pediu quatro vezes a mesma coisa e recebeu quatro "já mandei, te aviso
quando terminar". Nenhum dos três jobs chegou a responder, e nenhum morreu por
burrice do modelo — os três morreram em guardas nossas que não deixavam saída:

* **`7fd73dca`, 11s** — a Amazon redirecionou a página de pedidos para o signin.
  O driver não tinha NENHUM tratamento para "caí numa tela de login": o modelo
  tentou preencher o e-mail quatro vezes, foi recusado por repetição, e a regra
  das quatro recusas matou o job.
* **`14578a69`, 54s** — a visão respondeu VAZIO na triagem do carrinho
  «Subtotal», a triagem caiu no default `conteudo`, e o prompt DECLAROU ao
  modelo que o carrinho era a resposta e que era proibido fechá-lo. Depois
  disso, `look` e `extract` se recusaram alternadamente — e cada recusa mandava
  usar a outra. Cinco recusas, job morto, com a página certa aberta na tela.
* **`23cde731`, 8s** — abriu aba nova apesar de a tarefa dizer "na aba que já
  está aberta", porque a sessão de aba tinha sido apagada pelo registro dos
  prints (ver `test_sessao_de_aba.py`).

A lição é a mesma de 2026-08-29 em forma nova: **recusa sem alternativa vira
aborto**. Lá as recusas não ofereciam saída; aqui cada uma oferecia — e a saída
de uma era a outra.
"""

from __future__ import annotations

import json

import pytest

from core.browser_local.driver import BrowserLocalDriver


class _Resposta:
    def __init__(self, content: str) -> None:
        self._content = content

    def get_content(self) -> str:
        return self._content


class _Provider:
    def __init__(self, steps: list[dict], answer: str | None = None) -> None:
        self.steps = list(steps)
        self.answer = answer
        self.prompts: list[str] = []

    async def complete(self, **kwargs):
        self.prompts.append(kwargs["messages"][-1].content)
        if self.answer is not None:
            return _Resposta(self.answer)
        passo = self.steps.pop(0) if self.steps else {"action": "look", "args": {"pergunta": "?"}}
        return _Resposta(json.dumps(passo))


class _LLM:
    def __init__(self, by_model: dict) -> None:
        self.by_model = by_model

    def get_provider_for_model(self, model: str):
        return self.by_model[model]


def _tela(url: str = "https://loja.exemplo/pedido/1", **over):
    base = {
        "url": url,
        "title": "Detalhes do pedido",
        "ready": "complete",
        "stop": False,
        "elements": [
            {"ref": 0, "tag": "a", "text": "Rastrear pacote"},
            {"ref": 1, "tag": "button", "text": "Ver detalhes"},
        ],
        "text": "Detalhes do pedido",
    }
    base.update(over)
    return base


def _driver(steps, *, telas=None, olho_responde="o painel tem coisas", **kwargs):
    fila = list(telas) if telas else [_tela()]

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 7}
        if action == "browser.snapshot":
            return fila.pop(0) if len(fila) > 1 else fila[0]
        if action == "browser.extract":
            return {"ok": True, "text": "mesmo texto de sempre " * 20, "label": "Subtotal"}
        if action == "browser.screenshot":
            return {"ok": True, "data": "FOTO", "marks": True}
        return {"ok": True}

    olho = _Provider([], answer=olho_responde)
    texto = _Provider(steps)
    driver = BrowserLocalDriver(
        act=act,
        llm=_LLM({"visao": olho, "texto": texto}),
        model="texto",
        vision_model="visao",
        **kwargs,
    )
    driver._texto = texto  # type: ignore[attr-defined]
    return driver


@pytest.mark.asyncio
async def test_duas_recusas_de_leitura_mandam_agir_em_vez_de_ler():
    """O ping-pong do job 14578a69.

    `look` recusado dizia "use `extract`"; `extract` recusado dizia "leia do
    snapshot ou escolha outro caminho". O modelo foi de uma à outra cinco vezes.
    A partir da segunda na mesma tela, a mensagem para de sugerir outra forma de
    LER e nomeia o que dá para FAZER.
    """
    driver = _driver(
        [
            {"action": "look", "args": {"pergunta": "o que tem aqui?"}},
            {"action": "look", "args": {"pergunta": "e agora?"}},
            {"action": "extract", "args": {"ref": 0}},
            {"action": "extract", "args": {"ref": 0}},
            {"action": "done", "args": {"answer": "pronto"}},
        ]
    )

    result = await driver.run("qual o status do pedido?")

    recusas = [s.detail or "" for s in result.steps if s.args.get("recusado")]
    assert recusas, "nenhuma recusa aconteceu — o teste não exercitou nada"
    escalou = [r for r in recusas if "LER não é a saída" in r]
    assert escalou, f"a recusa não escalou: {recusas}"
    # Recusa que não nomeia a saída vira aborto — a lição de 2026-08-29.
    assert "Opções agora" in escalou[0]
    assert "`done`" in escalou[0], "faltou a saída de encerrar com o que já se tem"


@pytest.mark.asyncio
async def test_triagem_que_falhou_nao_declara_o_painel_como_resposta():
    """A visão respondeu VAZIO e o carrinho virou "a resposta".

    O default cauteloso (`conteudo`) continua certo para o que vem DEPOIS — quem
    fecha a resposta paga caro. O que estava errado era o prompt: ele mandava
    não fechar e dizia que a resposta estava ali dentro, em cima de um palpite.
    A própria guarda de fechamento já discordava (ela só recusa com `fonte` de
    evidência), ou seja, só o texto mentia.
    """
    com_painel = _tela(
        elements=[
            {"ref": 0, "tag": "a", "text": "Rastrear pacote"},
            {"ref": 9, "tag": "div", "text": "Subtotal R$ 75,77", "menu": True,
             "panel": "Subtotal", "raiz": True, "texto": "Subtotal R$ 75,77"},
        ]
    )
    driver = _driver([])
    paineis = driver._panels(com_painel)
    assert paineis, "a fixture não produziu painel nenhum — o teste seria vazio"

    # `fonte: erro` = a triagem NÃO rodou (foi o caso real: visão vazia).
    falhou = {
        k: {"tipo": "conteudo", "porque": "a visão respondeu VAZIO", "fonte": "erro"}
        for k in paineis
    }
    render = driver._render(com_painel, falhou)
    assert "NÃO consegui classificar" in render
    assert "a resposta está AQUI DENTRO" not in render
    assert "pode fechar e seguir" in render

    # E o texto forte continua existindo para quando a triagem REALMENTE leu —
    # é ele que impede fechar a resposta, que é o erro caro do outro lado.
    viu = {
        k: {"tipo": "conteudo", "porque": "é o resultado do seu clique", "fonte": "visao"}
        for k in paineis
    }
    assert "a resposta está AQUI DENTRO" in driver._render(com_painel, viu)


@pytest.mark.asyncio
async def test_parede_de_login_para_em_vez_de_insistir():
    """O job 7fd73dca: 4 tentativas de preencher o e-mail, 11s, morto.

    Credencial não se arranja insistindo. Uma rodada é acidente de redirect (o
    modelo ainda pode voltar ou ir por outro caminho); a segunda é parede.
    """
    login = _tela(
        url="https://www.amazon.com.br/ap/signin?openid.return_to=x",
        title="Fazer login",
        elements=[{"ref": 2, "tag": "input", "text": "Insira seu e-mail"}],
    )
    driver = _driver(
        [{"action": "fill", "args": {"ref": 2, "value": "«pks:email»"}} for _ in range(10)],
        telas=[login],
    )

    result = await driver.run("vê meus pedidos na amazon")

    assert result.status == "failed"
    assert "pediu LOGIN" in result.summary
    assert driver._acting_steps <= 2, f"insistiu {driver._acting_steps} vezes na parede"
    # E o modelo foi AVISADO antes de a parada disparar.
    assert any("tela de LOGIN" in p for p in driver._texto.prompts)


@pytest.mark.asyncio
async def test_pagina_normal_nao_e_confundida_com_login():
    """A régua é o caminho da URL, não a palavra "login" em qualquer lugar."""
    driver = _driver(
        [{"action": "done", "args": {"answer": "pronto"}}],
        telas=[_tela(url="https://loja.exemplo/ajuda/como-fazer-login-no-app")],
    )

    result = await driver.run("lê a página de ajuda")

    assert result.status == "completed"
    assert "pediu LOGIN" not in result.summary
