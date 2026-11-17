"""Qual print chega ao chat — e por que não era o certo (job 78d998ee).

Em 2026-09-29 o Neo foi ver na Amazon o que estava a caminho, **respondeu
certo** ("Produto Exemplo, chega 5 de outubro, R$ 49,86") e anexou o print da página
de rastreio de OUTRO pedido, com o carrinho aberto por cima. Nada tinha
falhado: a lista que vira anexo era alimentada em dois lugares só — a triagem
de painel e a ação `screenshot` — e a tarefa inteira usou `look`, que fotografa,
pergunta à visão e **joga a imagem fora**. O agente nunca chamou `screenshot`,
então a única imagem que chegou ao usuário foi a de um painel que abriu por
acaso, na página errada. O print certo existia no diário e não foi entregue.

Somava um segundo erro: a lista guardava os PRIMEIROS prints. Numa tarefa de
navegador a evidência se acumula — a tela que responde a pergunta é a do fim.

As três promessas daqui são as três pontas disso.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from core.browser_local import driver as driver_mod
from core.browser_local.driver import BrowserLocalDriver


class _Resposta:
    def __init__(self, content: str) -> None:
        self._content = content

    def get_content(self) -> str:
        return self._content


class _Provider:
    """Dubla os dois papéis: devolve o roteiro, ou texto quando é o olho."""

    def __init__(self, steps: list[dict] | None = None, answer: str | None = None) -> None:
        self.steps = list(steps or [])
        self.answer = answer
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.answer is not None:
            return _Resposta(self.answer)
        passo = (
            self.steps.pop(0)
            if self.steps
            else {"action": "give_up", "args": {"reason": "sem roteiro"}}
        )
        return _Resposta(json.dumps(passo))


class _LLM:
    def __init__(self, by_model: dict) -> None:
        self.by_model = by_model

    def get_provider_for_model(self, model: str):
        return self.by_model[model]


def _tela(marker: str = "a", **over):
    base = {
        "url": "https://www.amazon.com.br/gp/css/order-history",
        "title": "Seus pedidos",
        "ready": "complete",
        "stop": False,
        "elements": [{"ref": 0, "tag": "button", "text": "Rastrear pacote"}],
        "text": f"Seus pedidos ({marker})",
    }
    base.update(over)
    return base


def _driver(steps, *, telas=None, pendura_print=0.0, **kwargs):
    """Driver cujos prints saem TODOS DIFERENTES.

    É a única forma de provar *qual* imagem chegou: com o dublê devolvendo
    sempre os mesmos bytes, a deduplicação da entrega esconderia a resposta.
    """
    fotos: list[dict] = []
    fila = list(telas or [_tela(c) for c in "abcdefgh"])

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 7}
        if action == "browser.snapshot":
            return fila.pop(0) if len(fila) > 1 else fila[0]
        if action == "browser.screenshot":
            if pendura_print:
                await asyncio.sleep(pendura_print)
            fotos.append(dict(args))
            return {
                "ok": True,
                "data": f"FOTO-{len(fotos)}",
                "marks": args.get("marks", True),
            }
        return {"ok": True}

    olho = _Provider(answer="a lista mostra os pedidos")
    texto = _Provider(steps)
    driver = BrowserLocalDriver(
        act=act,
        llm=_LLM({"visao": olho, "texto": texto}),
        model="texto",
        vision_model="visao",
        **kwargs,
    )
    driver._fotos = fotos  # type: ignore[attr-defined]
    return driver


@pytest.mark.asyncio
async def test_o_que_o_modelo_olhou_chega_ao_chat():
    """`look` é a tela sobre a qual ALGUÉM teve uma pergunta.

    É a melhor candidata a print de entrega que existe no meio da tarefa, e era
    a única que não tinha como chegar lá.
    """
    driver = _driver(
        [
            {"action": "look", "args": {"pergunta": "o que tem aqui?"}},
            {"action": "done", "args": {"answer": "achei o Produto Exemplo"}},
        ]
    )

    result = await driver.run("ver o que está a caminho")

    assert "FOTO-1" in result.screenshots, "a imagem que o modelo olhou tem que sair daqui"


@pytest.mark.asyncio
async def test_a_janela_guarda_os_ultimos_prints_e_nao_os_primeiros():
    """O erro que entregou o pedido errado.

    Com teto 2 e quatro olhadas, o que o usuário precisa ver é o que a tarefa
    viu por último — foi de onde saiu a resposta. Guardar os primeiros entrega
    o caminho e esconde o destino.
    """
    driver = _driver(
        [
            # Rolar entre as olhadas: sem uma ação no meio, a segunda seria recusada
            # (página que muda sozinha não justifica olhar de novo).
            {"action": "look", "args": {"pergunta": "1?"}},
            {"action": "scroll", "args": {"delta": 500}},
            {"action": "look", "args": {"pergunta": "2?"}},
            {"action": "scroll", "args": {"delta": 500}},
            {"action": "look", "args": {"pergunta": "3?"}},
            {"action": "scroll", "args": {"delta": 500}},
            {"action": "look", "args": {"pergunta": "4?"}},
            {"action": "done", "args": {"answer": "pronto"}},
        ],
        max_screenshots=2,
    )

    result = await driver.run("ver o que está a caminho")
    ultimo_do_meio = f"FOTO-{len(driver._fotos) - 1}"

    assert len(result.screenshots) == 2, "o teto continua valendo"
    assert result.screenshots[1] == ultimo_do_meio, "sobra o do fim, não o do começo"
    assert "FOTO-1" not in result.screenshots
    assert "FOTO-2" not in result.screenshots


@pytest.mark.asyncio
async def test_o_print_do_fim_vai_na_frente_e_sem_marcacoes():
    """A tela de onde saiu a resposta é a única que se sabe relevante.

    Vai primeiro (se o teto só deixar passar uma, que seja essa) e vai limpa:
    os números desenhados são andaime do modelo de visão e, para quem lê o
    chat, só sujam a foto.
    """
    driver = _driver(
        [
            {"action": "look", "args": {"pergunta": "o que tem aqui?"}},
            {"action": "done", "args": {"answer": "achei o Produto Exemplo"}},
        ]
    )

    result = await driver.run("ver o que está a caminho")

    assert result.screenshots[0] == f"FOTO-{len(driver._fotos)}", "o do fim vem na frente"
    assert driver._fotos[-1].get("marks") is False, "o print de quem lê não leva os números"
    assert driver._fotos[0].get("marks") is True, "o da visão continua marcado"
    assert result.screenshot_captions[0] == "a tela quando terminei"
    assert result.screenshot_captions[1:] == ["no caminho"] * (len(result.screenshots) - 1)


@pytest.mark.asyncio
async def test_tarefa_que_acaba_mal_tambem_entrega_onde_parou():
    """É no fim ruim que a foto vale mais.

    `give_up` e orçamento estourado eram exatamente os casos em que o usuário
    ficava com um texto de desculpa e nenhuma tela.
    """
    driver = _driver([{"action": "give_up", "args": {"reason": "não achei a lista"}}])

    result = await driver.run("ver o que está a caminho")

    assert result.status == "failed"
    assert result.screenshots, "desistir não é motivo para não mostrar onde parou"
    assert result.screenshot_captions[0] == "a tela quando terminei"


@pytest.mark.asyncio
async def test_o_print_do_fim_nao_segura_a_resposta(monkeypatch):
    """Bônus não atrasa entrega.

    `browser.screenshot` tem 25s de teto. Numa tarefa que morreu porque o
    device sumiu, pagar esse teto seria fazer o usuário esperar por uma foto
    que não vem — em cima de um fracasso que já aconteceu.
    """
    monkeypatch.setattr(driver_mod, "_TIMEOUT_PRINT_FINAL", 0.05)
    driver = _driver(
        [{"action": "done", "args": {"answer": "pronto"}}], pendura_print=5.0
    )

    comecou = time.monotonic()
    result = await driver.run("ver o que está a caminho")

    assert time.monotonic() - comecou < 2.0, "não pode pagar o teto do screenshot"
    assert result.status == "completed", "a resposta sai mesmo sem a foto"
    assert result.screenshots == []
