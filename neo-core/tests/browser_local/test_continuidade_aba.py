"""Continuidade de aba entre tarefas, e a permissão que vem junto.

O caso (2026-09-01): o agente chegou até o arquivo, parou para pedir
confirmação de exclusão, o usuário autorizou — e a tarefa seguinte recomeçou
em `about:blank`, com o cérebro reimprovisando o texto. Três jobs seguidos
morreram assim.

`browser.release` DETACHA mas não fecha a aba: ela continua aberta na página.
O que se perdia era só o id. Aqui estão as três peças do conserto:

1. o driver reivindica a aba anterior quando ela ainda serve;
2. o runner registra onde parou (aba + clique pendente);
3. a autorização é amarrada AO CLIQUE, não ao job inteiro.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.browser_local.driver import BrowserLocalDriver, NeedsConfirmation

from tests.browser_local.test_driver_vision import _FakeLLM, _FakeProvider, _snapshot


# ── 3. a permissão é do CLIQUE, não do job ──────────────────────────────


def _driver_sensivel(**kw):
    return BrowserLocalDriver(
        act=lambda *a, **k: None,
        llm=_FakeLLM({}, _FakeProvider([])),
        model="texto",
        **kw,
    )


def test_sem_autorizacao_qualquer_acao_perigosa_para():
    driver = _driver_sensivel()
    with pytest.raises(NeedsConfirmation):
        driver._check_sensitive("Excluir definitivamente")


def test_autorizacao_ampla_continua_valendo_como_antes():
    """`confirm_sensitive_ok` sem retomada mantém o comportamento antigo."""
    driver = _driver_sensivel(allow_sensitive=True)
    driver._check_sensitive("Excluir definitivamente")  # não levanta
    driver._check_sensitive("Comprar agora")  # idem — é um cheque em branco


def test_retomada_libera_so_o_clique_autorizado():
    """O ponto da F3: 'pode excluir ESTE arquivo' não vira 'pode tudo'."""
    driver = _driver_sensivel(
        allow_sensitive=True, allow_sensitive_label="Excluir definitivamente"
    )
    driver._check_sensitive("Excluir definitivamente")  # o que foi autorizado
    with pytest.raises(NeedsConfirmation):
        driver._check_sensitive("Comprar agora")  # outra coisa: para de novo


def test_o_rotulo_autorizado_ignora_caixa_e_espaco():
    driver = _driver_sensivel(allow_sensitive_label="excluir definitivamente")
    driver._check_sensitive("  Excluir Definitivamente  ")


def test_acao_inocente_nunca_para():
    driver = _driver_sensivel(allow_sensitive_label="Excluir definitivamente")
    driver._check_sensitive("Abrir pasta")
    driver._check_sensitive("")


# ── 1. reivindicar a aba anterior ───────────────────────────────────────


def _driver_com_abas(abas, *, tab_ref=None, steps=None, acted=None):
    acted = acted if acted is not None else []

    async def act(action, args, timeout):
        acted.append((action, dict(args)))
        if action == "browser.claim":
            # O id volta do que foi pedido — é assim que o teste vê se reusou.
            return {"tab_ref": args.get("tab_id", 999)}
        if action == "browser.tabs":
            return {"tabs": abas}
        if action == "browser.snapshot":
            return _snapshot()
        if action == "browser.screenshot":
            return {"ok": True, "format": "jpeg", "data": "eA==", "marks": True}
        return {"ok": True}

    driver = BrowserLocalDriver(
        act=act,
        llm=_FakeLLM({}, _FakeProvider(steps or [{"action": "done", "args": {"answer": "ok"}}])),
        model="texto",
        tab_ref=tab_ref,
    )
    driver._acted = acted  # type: ignore[attr-defined]
    return driver


@pytest.mark.asyncio
async def test_aba_viva_e_reivindicada_em_vez_de_abrir_nova():
    acted: list = []
    driver = _driver_com_abas(
        [{"tabId": 77, "url": "https://drive.google.com/drive/trash"}],
        tab_ref=77,
        acted=acted,
    )
    result = await driver.run("continua de onde parou")
    claim = next(a for k, a in acted if k == "browser.claim")
    assert claim.get("tab_id") == 77, "devia reivindicar a aba anterior"
    assert result.tab_ref == 77, "e o resultado tem que dizer onde parou"


@pytest.mark.asyncio
async def test_aba_fechada_cai_para_aba_nova_sem_falhar():
    """O usuário fecha aba o tempo todo — isso não pode derrubar o job."""
    acted: list = []
    driver = _driver_com_abas([{"tabId": 12, "url": "https://outra.com"}], tab_ref=77, acted=acted)
    result = await driver.run("continua")
    claim = next(a for k, a in acted if k == "browser.claim")
    assert "tab_id" not in claim
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_listar_abas_falhando_cai_para_aba_nova():
    async def act(action, args, timeout):
        if action == "browser.tabs":
            raise RuntimeError("device mudo")
        if action == "browser.claim":
            return {"tab_ref": args.get("tab_id", 999)}
        if action == "browser.snapshot":
            return _snapshot()
        return {"ok": True}

    driver = BrowserLocalDriver(
        act=act,
        llm=_FakeLLM({}, _FakeProvider([{"action": "done", "args": {"answer": "ok"}}])),
        model="texto",
        tab_ref=77,
    )
    assert (await driver.run("continua")).status == "completed"


@pytest.mark.asyncio
async def test_aba_que_foi_parar_em_dominio_proibido_nao_e_reusada():
    acted: list = []
    driver = _driver_com_abas(
        [{"tabId": 77, "url": "https://banco.com/conta"}], tab_ref=77, acted=acted
    )
    driver.blocked = ("banco.com",)
    await driver.run("continua")
    claim = next(a for k, a in acted if k == "browser.claim")
    assert "tab_id" not in claim, "não herdamos uma aba que foi para domínio proibido"


@pytest.mark.asyncio
async def test_sem_aba_anterior_nada_muda():
    """Regressão: o caminho normal não pode nem chamar browser.tabs."""
    acted: list = []
    driver = _driver_com_abas([], acted=acted)
    await driver.run("tarefa nova")
    assert not [k for k, _ in acted if k == "browser.tabs"]
    assert "tab_id" not in next(a for k, a in acted if k == "browser.claim")


# ── 2. o que o job deixa registrado ─────────────────────────────────────


@pytest.mark.asyncio
async def test_parar_para_perguntar_devolve_a_aba_e_o_clique():
    acted: list = []

    async def act(action, args, timeout):
        acted.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": args.get("tab_id", 999)}
        if action == "browser.tabs":
            return {"tabs": [{"tabId": 77, "url": "https://drive.google.com"}]}
        if action == "browser.snapshot":
            # O rótulo que dispara a confirmação vem do SNAPSHOT (o que está na
            # tela), não do resultado do clique — é o que existe ANTES de tocar.
            return _snapshot(
                elements=[
                    {"ref": 0, "tag": "button", "text": "Cancelar"},
                    {"ref": 2, "tag": "button", "text": "Excluir definitivamente"},
                ]
            )
        return {"ok": True}

    driver = BrowserLocalDriver(
        act=act,
        llm=_FakeLLM({}, _FakeProvider([{"action": "click", "args": {"ref": 2}}])),
        model="texto",
        tab_ref=77,
    )
    result = await driver.run("apaga o arquivo")
    assert result.status == "needs_confirmation"
    assert result.pending == "Excluir definitivamente"
    assert result.tab_ref == 77, "sem a aba, a retomada recomeça do zero"


@pytest.mark.asyncio
async def test_o_formato_real_do_device_usa_id_e_nao_tabId():
    """`toUserTab` (background.js) devolve `id`. Um teste que só exercita
    `tabId` passaria com o código errado — este fixa o contrato de verdade."""
    acted: list = []
    driver = _driver_com_abas(
        [{"id": 77, "url": "https://drive.google.com/drive/trash", "title": "Drive"}],
        tab_ref=77,
        acted=acted,
    )
    await driver.run("continua de onde parou")
    assert next(a for k, a in acted if k == "browser.claim").get("tab_id") == 77
