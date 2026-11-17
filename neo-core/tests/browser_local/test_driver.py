"""BrowserLocalDriver — as travas que impedem o Neo de mentir ou de sair do escopo.

Isola o driver do canal (o ``act`` é um dublê) e do LLM (o provider é um dublê
que devolve os passos roteirizados). O que está sob teste é o julgamento do
driver, não a rede.
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
    """Devolve os passos na ordem em que foram roteirizados."""

    def __init__(self, steps: list[dict]) -> None:
        self.steps = list(steps)
        self.calls = 0

    async def complete(self, **kwargs):
        self.calls += 1
        step = self.steps.pop(0) if self.steps else {"action": "give_up", "args": {"reason": "sem roteiro"}}
        return _FakeResponse(json.dumps(step))


class _FakeLLM:
    def __init__(self, provider: _FakeProvider) -> None:
        self.provider = provider

    def get_provider_for_model(self, model: str):
        return self.provider


def _snapshot(**over):
    base = {
        "url": "https://exemplo.com/",
        "title": "Exemplo",
        "ready": "complete",
        "stop": False,
        "elements": [
            {"ref": 0, "tag": "button", "text": "Entrar"},
            {"ref": 1, "tag": "input", "type": "text", "text": "Busca", "value": ""},
            {"ref": 2, "tag": "button", "text": "Finalizar compra"},
        ],
        "text": "página de exemplo",
        # O device atual manda `main` SEMPRE (vale `null` quando a página não tem
        # conteúdo principal identificável). É por este campo que o driver sabe
        # que o desktop já foi reiniciado — ver `_snapshot`.
        "main": None,
    }
    base.update(over)
    return base


def _driver(steps, *, acted=None, snapshot=None, **kwargs):
    """Driver com canal dublê. ``acted`` recebe (action, args) de cada chamada."""
    acted = acted if acted is not None else []
    snap = snapshot or _snapshot()

    async def act(action, args, timeout):
        acted.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return snap
        if action == "browser.screenshot":
            return {"data": "ZmFrZQ=="}
        if action == "browser.navigate":
            return {"ok": True, "url": args.get("url")}
        if action == "browser.click":
            return {"ok": True, "label": "Entrar"}
        return {"ok": True}

    driver = BrowserLocalDriver(
        act=act,
        llm=_FakeLLM(_FakeProvider(steps)),
        model="fake",
        **kwargs,
    )
    driver._acted = acted  # type: ignore[attr-defined]
    return driver


@pytest.mark.asyncio
async def test_done_devolve_a_resposta_e_solta_a_aba():
    driver = _driver([{"action": "done", "args": {"answer": "O título é Exemplo"}}])
    result = await driver.run("me diga o título")
    assert result.status == "completed"
    assert "Exemplo" in result.summary
    # A aba SEMPRE é solta — debugger grudado estraga a aba do usuário.
    assert ("browser.release", {"tab_ref": 42}) in driver._acted


@pytest.mark.asyncio
async def test_falha_do_device_nao_vira_sucesso():
    """A lição da skill HA: 'cliquei' só é verdade com ok:true do device."""
    acted = []

    async def act(action, args, timeout):
        acted.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 7}
        if action == "browser.snapshot":
            return _snapshot()
        if action == "browser.click":
            raise RuntimeError("elemento desabilitado")
        return {"ok": True}

    driver = BrowserLocalDriver(
        act=act,
        llm=_FakeLLM(
            _FakeProvider(
                [
                    {"action": "click", "args": {"ref": 0}},
                    {"action": "done", "args": {"answer": "pronto"}},
                ]
            )
        ),
        model="fake",
    )
    result = await driver.run("clica em entrar")
    passos = [s for s in result.steps if s.action == "click"]
    assert passos and passos[0].ok is False
    # O texto entregue ao usuário ADMITE a falha — ele estava olhando a tela.
    assert "falhou" in result.summary or "falharam" in result.summary


@pytest.mark.asyncio
async def test_ref_fora_do_snapshot_nao_vira_comando():
    driver = _driver(
        [
            {"action": "click", "args": {"ref": 99}},
            {"action": "done", "args": {"answer": "ok"}},
        ]
    )
    result = await driver.run("clica em algo")
    assert not any(a == "browser.click" for a, _ in driver._acted)
    # O passo recusado guarda a ação TENTADA (não um "(inválido)" genérico): é
    # disso que o modelo precisa para se corrigir na rodada seguinte.
    recusado = next(s for s in result.steps if s.args.get("recusado"))
    assert recusado.action == "click" and not recusado.ok


@pytest.mark.asyncio
async def test_acao_inventada_e_recusada():
    driver = _driver(
        [
            {"action": "hackear", "args": {}},
            {"action": "done", "args": {"answer": "ok"}},
        ]
    )
    result = await driver.run("faz algo")
    assert any("não existe" in (s.detail or "") for s in result.steps)
    assert result.status == "completed"  # se recuperou no passo seguinte


@pytest.mark.asyncio
async def test_quatro_invalidos_seguidos_param_o_job():
    driver = _driver([{"action": "hackear", "args": {}}] * 5)
    result = await driver.run("faz algo")
    assert result.status == "failed"
    # O resumo carrega o MOTIVO da última recusa: sem ele não dá para saber se
    # travou por ref inventada, ação inexistente ou repetição barrada.
    assert "Travei" in result.summary
    assert "não existe" in result.summary


@pytest.mark.asyncio
async def test_dominio_proibido_nunca_e_navegado():
    driver = _driver(
        [
            {"action": "navigate", "args": {"url": "https://banco.com.br/login"}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        blocked_domains=("banco.com.br",),
    )
    await driver.run("entra no banco")
    assert not any(a == "browser.navigate" for a, _ in driver._acted)


@pytest.mark.asyncio
async def test_fora_da_allowlist_do_job_e_barrado():
    driver = _driver(
        [
            {"action": "navigate", "args": {"url": "https://exemplo.com/busca"}},
            {"action": "navigate", "args": {"url": "https://outrolugar.com/"}},
            {"action": "done", "args": {"answer": "ok"}},
        ]
    )
    result = await driver.run("procura no exemplo.com")
    navegou = [args["url"] for a, args in driver._acted if a == "browser.navigate"]
    assert "https://exemplo.com/busca" in navegou
    assert "https://outrolugar.com/" not in navegou
    assert any("fora do escopo" in (s.detail or "") for s in result.steps)


@pytest.mark.asyncio
async def test_pare_na_pagina_interrompe():
    driver = _driver(
        [{"action": "click", "args": {"ref": 0}}],
        snapshot=_snapshot(stop=True),
    )
    result = await driver.run("clica em entrar")
    assert result.status == "stopped"
    assert "PARE" in result.summary
    assert ("browser.release", {"tab_ref": 42}) in driver._acted


@pytest.mark.asyncio
async def test_acao_irreversivel_para_e_pergunta():
    driver = _driver([{"action": "click", "args": {"ref": 2}}])  # "Finalizar compra"
    result = await driver.run("finaliza o pedido")
    assert result.status == "needs_confirmation"
    assert "Finalizar compra" in result.summary
    assert not any(a == "browser.click" for a, _ in driver._acted)


@pytest.mark.asyncio
async def test_autorizacao_explicita_libera_a_acao_irreversivel():
    driver = _driver(
        [
            {"action": "click", "args": {"ref": 2}},
            {"action": "done", "args": {"answer": "comprei"}},
        ],
        allow_sensitive=True,
    )
    result = await driver.run("finaliza o pedido")
    assert result.status == "completed"
    assert any(a == "browser.click" for a, _ in driver._acted)


@pytest.mark.asyncio
async def test_teto_de_passos_encerra_com_onde_parou():
    driver = _driver([{"action": "scroll", "args": {"delta": 300}}] * 6, max_steps=3)
    result = await driver.run("rola a página")
    assert result.status == "failed"
    assert "exemplo.com" in result.url


@pytest.mark.asyncio
async def test_valor_digitado_nunca_aparece_no_historico():
    """O `value` pode ser senha — o resumo do passo nunca o ecoa."""
    driver = _driver(
        [
            {"action": "fill", "args": {"ref": 1, "value": "senha-secreta"}},
            {"action": "done", "args": {"answer": "ok"}},
        ]
    )
    result = await driver.run("preenche o campo")
    rendered = " ".join(s.render() for s in result.steps)
    assert "senha-secreta" not in rendered
