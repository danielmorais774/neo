"""Painel que abre: é a RESPOSTA ou é um INTRUSO?

Escrito a partir do job 6785cf70 (2026-09-06), que queimou os 25 passos numa
página de rastreio da Amazon. O modelo clicou em "Ver todas as atualizações", o
site abriu um modal com a timeline inteira — que ERA a tarefa — e o driver
informou, no mesmo prompt, três coisas que empurravam para o lado errado:

* ``a tela NÃO mudou`` (o fingerprint era cego a modal);
* ``⟪COBERTO por⟫`` em ~30 elementos, com a regra "dispense o que está na frente";
* nenhuma pista de que o painel tinha nascido do próprio clique dele.

Resultado: 7 aberturas, 11 tentativas de fechar, e a resposta extraída no passo
19 jogada fora no passo 20. Cada teste aqui trava um degrau desse caminho.
"""

from __future__ import annotations

import json

import pytest

from core.browser_local.driver import BrowserLocalDriver


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self._content = content

    def get_content(self) -> str:
        return self._content


class _FakeProvider:
    def __init__(self, steps: list[dict] | None = None, answer: str | None = None) -> None:
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


#: O texto que estava dentro do modal da Amazon e que nunca chegou ao prompt.
TIMELINE = (
    "Enviado por Amazon Logistics Brazil\n"
    "Código de rastreamento: TBR424478221\n"
    "domingo 6 setembro\n1:50 AM\n"
    "Pacote chegou a uma instalação da transportadora.\nSobral, BR\n"
    "sexta-feira 4 setembro\n3:19 AM\n"
    "O pacote saiu de uma instalação da Amazon.\n"
    "Cabo de Santo Agostinho, Pernambuco BR"
)


def _base(**over):
    base = {
        "url": "https://www.amazon.com.br/gp/your-account/ship-track?orderId=702",
        "title": "Rastrear pedido",
        "ready": "complete",
        "stop": False,
        "truncated": True,
        "text": "Ir para\nConteúdo principal\nAtalhos do teclado\nPesquisar",
        "elements": [],
    }
    base.update(over)
    return base


def _fechado():
    """A página de rastreio com o link, sem modal."""
    return _base(
        elements=[
            {"ref": 50, "tag": "a", "text": "Ver todas as atualizações"},
            {"ref": 53, "tag": "a", "text": "Ver os detalhes do pedido"},
        ]
    )


def _aberto(rotulo: str = "Enviado por Amazon Logistics Brazil", texto: str = TIMELINE):
    """A mesma página com o modal aberto — 200 elementos, lista cortada.

    Os 198 primeiros são a moldura da Amazon, idêntica com e sem modal: é a
    reprodução fiel do que cegava o fingerprint (`elements[:80]` e `text[:1500]`
    só amostram o topo, e `len(elements)` satura no teto de 200).
    """
    moldura = [
        {"ref": i, "tag": "a", "text": f"link de moldura {i}", "chrome": True}
        for i in range(48)
    ]
    return _base(
        elements=[
            {"ref": 50, "tag": "a", "text": "Ver todas as atualizações",
             "coberto": rotulo, "coberto_painel": True},
            {"ref": 53, "tag": "a", "text": "Ver os detalhes do pedido",
             "coberto": rotulo, "coberto_painel": True},
            *moldura,
            {"ref": 187, "tag": "div", "type": "dialog", "text": rotulo[:40],
             "menu": True, "raiz": True, "panel": rotulo, "texto": texto,
             "box": [560, 140, 600, 596]},
            {"ref": 188, "tag": "button", "text": "Close", "menu": True, "panel": rotulo},
        ]
    )


# ── 1. o fingerprint enxerga o modal ────────────────────────────────────────


def test_abrir_modal_conta_como_tela_que_mudou():
    """O defeito de origem: 16 passos carimbados "NÃO mudou" com o modal na tela."""
    assert BrowserLocalDriver._fingerprint(_fechado()) != BrowserLocalDriver._fingerprint(_aberto())


def test_dois_modais_diferentes_tem_assinaturas_diferentes():
    a = BrowserLocalDriver._fingerprint(_aberto("Enviado por Amazon", "chegou em Sobral"))
    b = BrowserLocalDriver._fingerprint(_aberto("Enviado por Amazon", "saiu de Sobral"))
    assert a != b


def test_lista_saturada_nao_esconde_mais_o_que_nasce_no_fim():
    """`len(elements)` empata em 200; quem tem de ver a diferença é o fim da lista."""
    antes = _base(elements=[{"ref": i, "tag": "a", "text": f"x{i}"} for i in range(200)])
    depois = _base(
        elements=[
            *[{"ref": i, "tag": "a", "text": f"x{i}"} for i in range(198)],
            {"ref": 198, "tag": "div", "type": "dialog", "text": "Aviso",
             "menu": True, "raiz": True, "panel": "Aviso", "texto": "algo novo"},
            {"ref": 199, "tag": "button", "text": "Close", "menu": True, "panel": "Aviso"},
        ]
    )
    assert len(antes["elements"]) == len(depois["elements"]) == 200
    assert BrowserLocalDriver._fingerprint(antes) != BrowserLocalDriver._fingerprint(depois)


# ── 2. o conteúdo do painel chega ao prompt ─────────────────────────────────


def test_o_texto_do_painel_vai_inteiro_para_o_prompt():
    """Sem isto, "não feche" só trocaria o loop por uma parede."""
    render = BrowserLocalDriver._render(_aberto())
    assert "TEXTO COMPLETO DO PAINEL" in render
    assert "Pacote chegou a uma instalação da transportadora." in render
    assert "Cabo de Santo Agostinho" in render


def test_elemento_atras_do_painel_nao_manda_mais_fechar():
    render = BrowserLocalDriver._render(_aberto())
    assert "atrás do painel aberto" in render
    assert 'COBERTO por "Enviado' not in render


# ── 3. triagem: causalidade decide de graça ─────────────────────────────────


def _driver(roteiro, *, vision=None, snapshots=None):
    acted: list = []
    fila = list(snapshots or [])

    async def act(action, args, timeout):
        acted.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return fila.pop(0) if fila else _fechado()
        if action == "browser.screenshot":
            return {"ok": True, "data": "Zm90bw==", "marks": False, "clipped": True}
        if action == "browser.extract":
            return {"ok": True, "text": TIMELINE}
        return {"ok": True, "label": "Ver todas as atualizações"}

    olhos = _FakeProvider(answer=vision) if vision else None
    driver = BrowserLocalDriver(
        act=act,
        llm=_FakeLLM({"olhos": olhos} if olhos else {}, _FakeProvider(roteiro)),
        model="texto",
        vision_model="olhos" if olhos else None,
    )
    return driver, acted


@pytest.mark.asyncio
async def test_painel_que_nasce_do_proprio_clique_e_conteudo_sem_gastar_visao():
    """O discriminador de graça: propaganda não espera você clicar."""
    driver, _ = _driver(
        [
            {"action": "click", "args": {"ref": 50}},
            {"action": "done", "args": {"answer": "fim"}},
        ],
        vision="{}",
        snapshots=[_fechado(), _aberto(), _aberto()],
    )
    await driver.run("veja a timeline de rastreio")
    veredito = next(iter(driver._panel_verdict.values()))
    assert veredito["tipo"] == "conteudo"
    assert veredito["fonte"] == "causa"
    assert driver._triagens == 0  # não pagou visão para isso


@pytest.mark.asyncio
async def test_fechar_o_painel_que_e_a_resposta_e_recusado():
    """A guarda central: no job real isto aconteceu 11 vezes sem uma recusa."""
    driver, acted = _driver(
        [
            {"action": "click", "args": {"ref": 50}},
            {"action": "click", "args": {"ref": 188}},   # "Close"
            {"action": "done", "args": {"answer": "fim"}},
        ],
        snapshots=[_fechado(), _aberto(), _aberto(), _aberto()],
    )
    result = await driver.run("veja a timeline de rastreio")
    clicados = [args.get("ref") for a, args in acted if a == "browser.click"]
    assert 188 not in clicados
    recusa = next(s for s in result.steps if s.args.get("recusado"))
    assert "não feche o painel" in recusa.detail
    assert "TEXTO COMPLETO DO PAINEL" in recusa.detail


@pytest.mark.asyncio
async def test_escape_tambem_conta_como_tentativa_de_fechar():
    driver, _ = _driver(
        [
            {"action": "click", "args": {"ref": 50}},
            {"action": "press", "args": {"key": "Escape"}},
            {"action": "done", "args": {"answer": "fim"}},
        ],
        snapshots=[_fechado(), _aberto(), _aberto(), _aberto()],
    )
    result = await driver.run("veja a timeline de rastreio")
    assert any("não feche o painel" in (s.detail or "") for s in result.steps)


@pytest.mark.asyncio
async def test_painel_sem_evidencia_nenhuma_pode_ser_fechado():
    """Sem visão e sem causa, a guarda não bloqueia: palpite não vira prisão."""
    driver, acted = _driver(
        [
            {"action": "click", "args": {"ref": 188}},
            {"action": "done", "args": {"answer": "fim"}},
        ],
        snapshots=[_aberto(), _aberto()],  # o painel JÁ estava aberto na 1ª tela
    )
    await driver.run("fecha esse painel aí")
    assert 188 in [args.get("ref") for a, args in acted if a == "browser.click"]


# ── 4. triagem visual: o caso que o DOM não resolve ─────────────────────────


@pytest.mark.asyncio
async def test_painel_que_aparece_sozinho_vai_para_a_triagem_visual():
    veredito_visao = json.dumps(
        {"tipo": "intruso", "porque": "é um anúncio de assinatura",
         "fechar": "Agora não"}
    )
    driver, acted = _driver(
        [{"action": "done", "args": {"answer": "fim"}}],
        vision=veredito_visao,
        # Rótulo NEUTRO de propósito: se o léxico o pegasse, a camada barata
        # resolveria e a visão nunca seria chamada — que é justamente o que
        # aconteceu com "Assine o Prime" na primeira versão deste teste.
        snapshots=[_aberto("Só hoje", "Leve 3 pague 2 em itens selecionados")],
    )
    await driver.run("veja a timeline")
    assert driver._triagens == 1
    veredito = next(iter(driver._panel_verdict.values()))
    assert veredito["tipo"] == "intruso"
    assert veredito["fechar"] == "Agora não"
    # O print da triagem sai RECORTADO no painel.
    clip = next(args for a, args in acted if a == "browser.screenshot").get("clip")
    assert clip == [560.0, 140.0, 600.0, 596.0]


@pytest.mark.asyncio
async def test_veredito_de_intruso_manda_fechar_pelo_texto_nunca_por_ref():
    """O ref 48 do job real era a foto de um produto; a visão o citou como o X."""
    driver, _ = _driver(
        [{"action": "done", "args": {"answer": "fim"}}],
        vision=json.dumps({"tipo": "intruso", "porque": "anúncio", "fechar": "Agora não"}),
        snapshots=[_aberto("Só hoje", "Leve 3 pague 2")],
    )
    await driver.run("veja a timeline")
    render = BrowserLocalDriver._render(_aberto("Só hoje", "Leve 3 pague 2"), driver._panel_verdict)
    assert "É INTRUSO" in render
    assert 'click_text` com "Agora não"' in render


def test_veredito_de_conteudo_manda_ler_e_nao_fechar():
    vereditos = {
        "Enviado por Amazon Logistics Brazil|600x596": {
            "tipo": "conteudo", "porque": "apareceu junto com a sua ação", "fonte": "causa",
        }
    }
    render = BrowserLocalDriver._render(_aberto(), vereditos)
    assert "É CONTEÚDO" in render
    assert "NÃO feche" in render


# ── 5. o que foi lido não se perde ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_substancial_vai_sozinho_para_o_caderno():
    """No job real a resposta foi extraída no passo 19 e morreu no histórico."""
    driver, _ = _driver(
        [
            {"action": "extract", "args": {"ref": 187}},
            {"action": "give_up", "args": {"reason": "acabou o roteiro"}},
        ],
        snapshots=[_aberto(), _aberto()],
    )
    result = await driver.run("veja a timeline de rastreio")
    assert any("Cabo de Santo Agostinho" in nota for nota in driver.notes)
    assert result.status == "failed"  # mesmo falhando, entrega o que leu
