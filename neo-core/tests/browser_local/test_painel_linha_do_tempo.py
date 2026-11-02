"""O painel da extensão recebe a linha do tempo — não uma frase trocada no lugar.

`docs/browser-local-plan.md` (2026-10-30, §"A faixa virou painel"). A faixa
antiga tinha UMA linha: cada passo apagava o anterior, e o que o agente
PRODUZIU (a leitura, a resposta da olhada com visão, o motivo da falha) não
tinha onde aparecer — cabia uma frase só. Quem estava olhando a própria aba
mexer sozinha via o presente e nada mais.

Agora cada evento é um item empilhado, com tipo. Este teste é a trava de que o
driver EMITE os quatro momentos (preparação, o passo, o retorno, o fim) — e a
de que o `value` do `fill` nunca entra em nenhum deles.
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
    def __init__(self, steps: list[dict]) -> None:
        self.steps = list(steps)

    async def complete(self, **kwargs):
        step = self.steps.pop(0) if self.steps else {"action": "give_up", "args": {"reason": "sem roteiro"}}
        return _FakeResponse(json.dumps(step))


class _FakeLLM:
    def __init__(self, provider: _FakeProvider) -> None:
        self.provider = provider

    def get_provider_for_model(self, model: str):
        return self.provider


def _snapshot():
    return {
        "url": "https://exemplo.com/",
        "title": "Exemplo",
        "ready": "complete",
        "stop": False,
        "elements": [
            {"ref": 0, "tag": "button", "text": "Entrar"},
            {"ref": 1, "tag": "input", "type": "password", "text": "Senha", "value": ""},
        ],
        "text": "página de exemplo",
        "main": None,
    }


def _driver(steps, *, banners, falha_no_click=False):
    async def act(action, args, timeout):
        if action == "browser.banner":
            banners.append(dict(args))
            return {"ok": True}
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return _snapshot()
        if action == "browser.click":
            if falha_no_click:
                raise RuntimeError("elemento desabilitado")
            return {"ok": True, "label": "Entrar"}
        return {"ok": True}

    return BrowserLocalDriver(
        act=act, llm=_FakeLLM(_FakeProvider(steps)), model="fake"
    )


@pytest.mark.asyncio
async def test_o_passo_e_o_retorno_sao_dois_itens():
    banners: list[dict] = []
    driver = _driver(
        [
            {"action": "click", "args": {"ref": 0}, "thought": "Vou entrar na conta."},
            {"action": "done", "args": {"answer": "entrei"}},
        ],
        banners=banners,
    )
    await driver.run("entra na conta")

    tipos = [b.get("kind") for b in banners]
    assert "fase" in tipos, "faltou a preparação"
    assert tipos.count("passo") >= 1, "o passo não foi para o painel"
    assert "ok" in tipos, "o RETORNO do passo não foi para o painel — é a metade que faltava"
    assert tipos[-1] == "fim", f"o fim não fecha a linha do tempo: {tipos}"

    passo = next(b for b in banners if b.get("kind") == "passo")
    # O raciocínio é a manchete; a ação concreta, a legenda. "click {'ref': 0}"
    # não diz nada a quem está olhando a aba.
    assert passo["text"] == "Vou entrar na conta."
    assert "clicando em «Entrar»" in passo["detail"]
    assert "passo 1/" in passo["detail"]

    # O retorno vem do DEVICE (o rótulo que ele leu), não da intenção do modelo.
    assert any(b.get("kind") == "ok" and "Entrar" in b.get("text", "") for b in banners)


@pytest.mark.asyncio
async def test_a_falha_vai_para_o_painel_com_o_motivo():
    banners: list[dict] = []
    driver = _driver(
        [
            {"action": "click", "args": {"ref": 0}, "thought": "Vou entrar."},
            {"action": "give_up", "args": {"reason": "o botão não funciona"}},
        ],
        banners=banners,
        falha_no_click=True,
    )
    await driver.run("entra na conta")

    erros = [b for b in banners if b.get("kind") == "erro"]
    assert erros, "a falha não chegou ao painel — a aba parece parada sem motivo"
    assert "elemento desabilitado" in erros[0]["detail"]
    assert "clicando em «Entrar»" in erros[0]["text"]


@pytest.mark.asyncio
async def test_o_que_foi_digitado_nunca_aparece_no_painel():
    """Mesma regra do `Step.render`: `value` pode ser senha, e o painel fica na
    TELA — e dentro de todo print que o agente tirar depois."""
    banners: list[dict] = []
    driver = _driver(
        [
            {
                "action": "fill",
                "args": {"ref": 1, "value": "senha-secreta-123"},
                "thought": "Preenchendo a senha.",
            },
            {"action": "done", "args": {"answer": "pronto"}},
        ],
        banners=banners,
    )
    await driver.run("faz login")

    assert banners, "nenhum evento foi emitido"
    for b in banners:
        inteiro = f"{b.get('text', '')} {b.get('detail', '')}"
        assert "senha-secreta-123" not in inteiro, f"vazou o que foi digitado: {b}"
    assert any("preenchendo" in b.get("detail", "").lower() for b in banners)


@pytest.mark.asyncio
async def test_o_detalhe_nao_estoura_o_storage_da_extensao():
    """A lista inteira é persistida no `storage.session` a cada evento."""
    banners: list[dict] = []
    driver = _driver([{"action": "done", "args": {"answer": "x" * 5000}}], banners=banners)
    await driver.run("resume a página")

    assert banners
    assert all(len(b.get("detail", "")) <= 1200 for b in banners)
