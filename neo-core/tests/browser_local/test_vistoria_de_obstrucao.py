"""A resposta da VISTORIA não é material de caderno.

Job `4a1e6748` (Amazon, "monitor OLED 27 polegadas"). Cinco olhadas; três
perguntaram preço e três acertaram. As outras duas perguntaram se havia um
painel de carrinho na frente — e foram as duas únicas notas ruins da entrega:

    "NÃO. Não há painel lateral de carrinho aberto. O botão X para fechá-lo não
     está visível, pois o carrinho não está expandido. A página do produto
     (preço, frete) está totalmente visível atrás — preço R$ 43,90, frete
     grátis, e todos os detalhes técnicos estão legíveis. Nenhum overlay ou
     modal cobre o conteúdo principal."

Esse "preço R$ 43,90" foi entregue ao usuário numa busca por monitores de
R$ 3.199,90. Ninguém perguntou preço ali: os olhos preencheram a frase. É a
regra que este arquivo tranca — **conteúdo que ninguém pediu é conteúdo que
ninguém conferiu**, e o caderno é o que vai para o usuário.

O portão julga a PRIMEIRA cláusula da pergunta. A primeira versão olhava a
frase inteira, via o "Onde está o botão X" da segunda cláusula, chamava de
pergunta aberta — e não pegava NENHUMA das duas vistorias reais. As três
perguntas boas do mesmo job abrem com "qual" e continuam passando.
"""

from __future__ import annotations

import json

import pytest

from core.browser_local.driver import BrowserLocalDriver

# ── as perguntas REAIS do job, palavra por palavra ──────────────────────────
P_VISTORIA_1 = (
    "Há um painel lateral de carrinho aberto? Onde está o botão X para fechá-lo? "
    "A página do produto (preço, frete) está visível atrás?"
)
P_VISTORIA_2 = (
    "Há um painel lateral de carrinho aberto cobrindo a página? "
    "Onde está o botão X/fechar dele?"
)
P_BOA_COM_ONDE = (
    "Qual o preço à vista, o preço parcelado e a informação de frete/entrega "
    "(prazo) mostrados nesta página de produto? Onde estão?"
)

# ── e as respostas REAIS ────────────────────────────────────────────────────
R_VISTORIA_1 = (
    "NÃO. Não há painel lateral de carrinho aberto. O botão X para fechá-lo não está "
    "visível, pois o carrinho não está expandido. A página do produto (preço, frete) "
    "está totalmente visível atrás — preço R$ 43,90, frete grátis, e todos os detalhes "
    "técnicos estão legíveis. Nenhum overlay ou modal cobre o conteúdo principal."
)
R_VISTORIA_2 = (
    "NÃO. Não há painel lateral de carrinho aberto cobrindo a página. O conteúdo "
    "principal está visível e não há overlays, modais ou banners bloqueando a "
    "visualização."
)
R_BOA = (
    "SIM. O preço à vista é R$ 3.199,90 (elemento 124). O parcelado é em até 12x de "
    "R$ 266,75 sem juros (elemento 124). A entrega é GRÁTIS e rápida, com prazo para "
    "quarta-feira, 11 de Novembro (elemento 13)."
)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self._content = content

    def get_content(self) -> str:
        return self._content


class _FakeProvider:
    def __init__(self, steps: list[dict] | None = None, *, answer: str | None = None) -> None:
        self.steps = list(steps or [])
        self.answer = answer
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.answer is not None:
            return _FakeResponse(self.answer)
        step = (
            self.steps.pop(0)
            if self.steps
            else {"action": "give_up", "args": {"reason": "sem roteiro"}}
        )
        return _FakeResponse(json.dumps(step))


class _FakeLLM:
    def __init__(self, by_model: dict[str, _FakeProvider], default: _FakeProvider) -> None:
        self.by_model = by_model
        self.default = default

    def get_provider_for_model(self, model: str):
        return self.by_model.get(model, self.default)


def _snapshot(marker: str = "a"):
    return {
        "url": "https://www.amazon.com.br/dp/B0GVPH73XN",
        "title": "Samsung Odyssey OLED G5 27",
        "ready": "complete",
        "stop": False,
        "elements": [
            {"ref": 0, "tag": "button", "text": "Adicionar ao carrinho"},
            {"ref": 1, "tag": "div", "text": f"R$ 3.199,90 ({marker})"},
        ],
        "text": f"Samsung Odyssey OLED G5 27 QHD 180Hz R$ 3.199,90 ({marker})",
        "main": None,
    }


def _driver(steps, *, olho, snapshots=None):
    queue = list(snapshots) if snapshots else [_snapshot()]
    texto = _FakeProvider(steps)

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return queue.pop(0) if len(queue) > 1 else queue[0]
        if action == "browser.screenshot":
            return {"ok": True, "data": "UFJJTlQ=", "marks": True}
        return {"ok": True}

    driver = BrowserLocalDriver(
        act=act,
        llm=_FakeLLM({"visao": olho, "texto": texto}, texto),
        model="texto",
        vision_model="visao",
    )
    driver._olho = olho  # type: ignore[attr-defined]
    return driver


def _driver_seco() -> BrowserLocalDriver:
    async def act(action, args, timeout):
        return {"ok": True}

    return BrowserLocalDriver(act=act, llm=_FakeLLM({}, _FakeProvider()), model="texto")


# ── 1. o caderno ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "pergunta,resposta",
    [(P_VISTORIA_1, R_VISTORIA_1), (P_VISTORIA_2, R_VISTORIA_2)],
)
def test_vistoria_nao_vira_nota(pergunta, resposta):
    driver = _driver_seco()
    driver._anotar_visao(resposta, pergunta)
    assert driver.notes == [], f"vistoria virou nota: {driver.notes}"


def test_o_preco_alucinado_da_vistoria_nao_chega_na_entrega():
    """O caso exato que o usuário leu: R$ 43,90 numa busca de R$ 3.199,90."""
    driver = _driver_seco()
    driver._anotar_visao(R_VISTORIA_1, P_VISTORIA_1)
    assert not any("43,90" in n for n in driver.notes)


def test_o_onde_da_segunda_clausula_nao_salva_a_vistoria():
    """Guarda da primeira tentativa de conserto: ela olhava a frase INTEIRA e o
    "Onde está o botão X" fazia a vistoria passar por pergunta aberta."""
    driver = _driver_seco()
    for pergunta, resposta in ((P_VISTORIA_1, R_VISTORIA_1), (P_VISTORIA_2, R_VISTORIA_2)):
        driver._anotar_visao(resposta, pergunta)
    assert driver.notes == []


def test_pergunta_boa_com_onde_continua_anotada():
    """A boa do mesmo job também tem "Onde estão?" — e tem que virar nota."""
    driver = _driver_seco()
    driver._anotar_visao(R_BOA, P_BOA_COM_ONDE)
    assert driver.notes and "3.199,90" in driver.notes[0]


def test_numero_da_marcacao_nao_vai_para_a_entrega():
    """`(elemento 124)` é conversa entre o driver e os olhos."""
    driver = _driver_seco()
    driver._anotar_visao(R_BOA, P_BOA_COM_ONDE)
    assert "elemento" not in driver.notes[0]
    assert driver.notes[0].startswith("a tela mostrava: O preço à vista")


def test_achado_que_comeca_com_nao_continua_anotado():
    """Guarda do job 29c739d4: "não há pedidos a caminho" ERA a resposta da tarefa."""
    driver = _driver_seco()
    driver._anotar_visao(
        "Não há pedidos A caminho nesta tela; todos os seis aparecem como Entregue.",
        "quais pedidos estão a caminho?",
    )
    assert driver.notes and "Entregue" in driver.notes[0]


def test_painel_que_e_a_resposta_continua_anotado():
    driver = _driver_seco()
    driver._anotar_visao(
        "O painel do carrinho mostra 3 itens: Kindle R$ 499, capa R$ 89 e "
        "carregador R$ 59, com total de R$ 647.",
        "qual o total no painel do carrinho?",
    )
    assert driver.notes and "647" in driver.notes[0]


def test_veredito_de_brinde_sai_da_resposta_boa():
    """Pergunta boa, resposta com "nenhum overlay cobre o conteúdo" no fim."""
    driver = _driver_seco()
    driver._anotar_visao(
        "O preço à vista é R$ 1.299,00 e o frete sai grátis para o CEP cadastrado. "
        "Nenhum overlay ou modal cobre o conteúdo principal.",
        "qual o preço e o frete?",
    )
    assert driver.notes
    assert "1.299,00" in driver.notes[0]
    assert "overlay" not in driver.notes[0]


# ── 2. ponta a ponta ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_vistoria_ainda_vai_para_os_olhos_mas_nao_suja_a_entrega():
    """O conserto é no CADERNO, não no `look`.

    Barrar a pergunta antes dos olhos custaria caro: a vistoria real vinha
    GRUDADA numa pergunta de conteúdo ("…a página do produto (preço, frete)
    está visível atrás?"), e responder pelo snapshot jogaria fora a metade que
    o modelo foi buscar. Quem filtra é a entrega, que é onde o estrago estava.
    """
    olho = _FakeProvider(answer=R_VISTORIA_1)
    driver = _driver(
        [
            {"action": "look", "args": {"pergunta": P_VISTORIA_1}},
            {"action": "done", "args": {"answer": "R$ 3.199,90"}},
        ],
        olho=olho,
    )
    result = await driver.run("preço do monitor OLED mais barato")

    olhada = next(s for s in result.steps if s.action == "look")
    assert "43,90" in olhada.detail, "o modelo tem que receber a resposta inteira"
    assert driver.notes == [], "a entrega não pode levar a vistoria"
    assert olho.calls, "o `look` continua sendo dos olhos"
