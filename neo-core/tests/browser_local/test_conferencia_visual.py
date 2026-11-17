"""A conferência visual: pergunta fechada por gatilho, veredito do código, e o
fluxo assíncrono no driver (docs/browser-visao-confirmacao-plan.md)."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

import shared.ajustes as ajustes_mod
from core.browser_local import conferencia as c
from tests.browser_local.test_visao_confirmacao import EMPRESA, FORM, SALVAR


def test_g2_valor_que_confere_e_o_que_entrou_cortado():
    ctx = {"campo": "Cargo", "valor": "Criador e desenvolvedor (projeto pessoal)"}
    assert "«Criador e desenvolvedor (projeto pessoal)»" in c.pergunta("G2", ctx)
    ok = c.interpretar("G2", ctx, '```json {"visto":"Criador e desenvolvedor (projeto pessoal)","confere":true,"erro_visivel":null,"coberto":false} ```')
    assert ok.ok is True and ok.linha.startswith("✓ «Cargo»")
    # campo estreito mostra o começo: vale
    assert c.interpretar("G2", ctx, '{"visto":"Criador e desenvolvedor (proj","erro_visivel":null}').ok is True
    # o código julga, não o "confere" da visão
    ruim = c.interpretar("G2", ctx, '{"visto":"Desenvolvedor / Criador","confere":true,"erro_visivel":null}')
    assert ruim.ok is False and "mostra «Desenvolvedor / Criador»" in ruim.divergencia


def test_g2_erro_vermelho_coberto_caixa_e_resposta_ruim():
    ctx = {"campo": "Até", "valor": "Novembro"}
    erro = c.interpretar("G2", ctx, '{"visto":"Mês","erro_visivel":"Campo obrigatório"}')
    assert erro.ok is False and "Campo obrigatório" in erro.linha
    assert c.interpretar("G2", ctx, '{"visto":"","coberto":true}').ok is False
    caixa = {"campo": "Trabalho aqui atualmente", "valor": None, "marcado": False}
    assert "DESMARCADA" in c.pergunta("G2", caixa)
    assert c.interpretar("G2", caixa, '{"visto":"desmarcada","confere":true}').ok is True
    assert c.interpretar("G2", ctx, "não consegui ver").ok is None


def test_g3_confere_os_campos_e_o_botao():
    ctx = {"botao": "SALVAR", "campos": [{"rotulo": "Empresa", "valor": "Acme"}, {"rotulo": "Telefone", "valor": None}]}
    q = c.pergunta("G3", ctx)
    assert "«Empresa» = «Acme»" in q and "«Telefone» preenchido" in q
    ok = c.interpretar("G3", ctx, json.dumps({"campos": [{"rotulo": "Empresa", "visto": "Acme"}], "erros_visiveis": [], "botao_habilitado": True}))
    assert ok.ok is True
    ruim = c.interpretar("G3", ctx, json.dumps({"campos": [{"rotulo": "Empresa", "visto": "Pontu"}], "erros_visiveis": ["Data inválida"], "botao_habilitado": False}))
    assert ruim.ok is False and "Data inválida" in ruim.linha and "desabilitado" in ruim.linha


def test_g4_g7_g8_g5_g1():
    assert c.interpretar("G4+G7", {"botao": "SALVAR"}, '{"resultado":"sucesso","mensagem":"Salvo"}').ok is True
    assert c.interpretar("G4", {}, '{"resultado":"formulario_aberto"}').ok is False
    assert c.interpretar("G7", {}, '{"pagina":"login","bloqueio":"login"}').ok is False
    assert c.interpretar("G8", {}, '{"previa_apareceu":true,"passo_extra":"recortar"}').linha.endswith("falta «recortar»")
    assert c.interpretar("G5", {}, '{"painel":"Foto","botoes":["Salvar","Cancelar"]}').ok is True
    assert c.interpretar("G1", {}, '{"erros_visiveis":[],"algo_cobrindo":"banner de cookies"}').ok is False


def test_valores_salvos_aparecem_na_dom():
    snap = {"tela": "Experiência\nCriador e desenvolvedor (projeto pessoal)\nNeo"}
    campos = [{"rotulo": "Cargo", "valor": "Criador e desenvolvedor (projeto pessoal)"}, {"rotulo": "Ano", "valor": "2026"}]
    assert c.valores_salvos_na_pagina(snap, campos) == ["Cargo"]


# ── o fluxo no driver ──────────────────────────────────────────────────────


class _Olho:
    """A visão falsa: responde pelo gatilho que a pergunta descreve."""

    def __init__(self, campo_visto="Acme", erro=None):
        self.campo_visto, self.erro = campo_visto, erro
        self.perguntas: list[str] = []

    async def complete(self, **kw):
        q = kw["messages"][-1].content
        self.perguntas.append(q)
        assert kw.get("teto_raciocinio") == 200 and kw["messages"][-1].images
        if "deve mostrar" in q:
            r = {"visto": self.campo_visto, "erro_visivel": self.erro, "coberto": False}
        elif "Antes de clicar" in q:
            r = {"campos": [{"rotulo": "Empresa", "visto": self.campo_visto}], "erros_visiveis": [], "botao_habilitado": True}
        elif "Acabou de clicar" in q:
            r = {"resultado": "sucesso", "mensagem": "Experiência salva"}
        else:
            r = {"erros_visiveis": [], "algo_cobrindo": None}
        from types import SimpleNamespace

        return SimpleNamespace(get_content=lambda: json.dumps(r))


@pytest.fixture
def visao_ligada(tmp_path, monkeypatch):
    arquivo = tmp_path / "neo.toml"
    arquivo.write_text(
        "[navegador.conversa]\nespera_min = 0.005\nespera_intervalo_s = 0.2\n"
        "[navegador.visao]\nconfirmacao_visual = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NEO_AJUSTES", str(arquivo))
    monkeypatch.setattr(ajustes_mod, "_cache", None)


def _rodar(olho, device):
    from tests.browser_local.test_conversa import _driver

    # O navegador de verdade leva centenas de ms por comando: é nesse tempo que
    # as conferências rodam. Sem ceder a vez, o asyncio nunca as agendaria.
    original = device.act

    async def devagar(action, args, timeout):
        await asyncio.sleep(0.01)
        return await original(action, args, timeout)

    device.act = devagar

    driver, ctx = _driver(
        [
            {"action": "fill", "args": {"ref": 3, "value": "Acme"}},
            {"action": "wait_for", "args": {"text": "Empresa"}},
            {"action": "click", "args": {"ref": 15}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        device,
        confirm_sensitive=False,
    )
    driver.vision_model = "olho"
    driver.llm.by_model["olho"] = olho
    return driver, ctx


@pytest.mark.asyncio
async def test_conferencias_chegam_ao_prompt_e_a_timeline(visao_ligada):
    from tests.browser_local.test_visao_confirmacao import _Formulario

    device = _Formulario()
    device.banners = []
    original = device.act

    async def act(action, args, timeout):
        if action == "browser.banner":
            device.banners.append(dict(args))
        return await original(action, args, timeout)

    device.act = act
    olho = _Olho()
    driver, ctx = _rodar(olho, device)
    await driver.run("cadastrar a experiência", start_url=FORM)

    linhas = [r["conferencia"].linha for r in driver.conferencias]
    assert any(l.startswith("✓ «Empresa» mostra «Acme»") for l in linhas)
    assert any("depois de enviar" in l and "sucesso" in l for l in linhas)
    # a página salva do teste não repete o valor: a conferência pela DOM diz isso
    assert any(l.startswith("? depois de enviar, nenhum valor digitado") for l in linhas)
    prompts = [call["messages"][-1].content for call in ctx.provider.calls]
    assert any("CONFERÊNCIA VISUAL" in p and "«Empresa» mostra «Acme»" in p for p in prompts)
    kinds = [b.get("kind") for b in device.banners]
    assert "print" in kinds and "visto" in kinds
    # print de tela inteira pede largura máxima, não escala
    assert any(p.get("max_width") == 1280 for p in device.prints)


@pytest.mark.asyncio
async def test_divergencia_vai_para_o_passo_que_causou(visao_ligada):
    from tests.browser_local.test_visao_confirmacao import _Formulario

    driver, ctx = _rodar(_Olho(campo_visto="Ponta", erro="Empresa inválida"), _Formulario())
    await driver.run("cadastrar", start_url=FORM)
    fill = next(s for s in driver.steps if s.action == "fill")
    assert "⚠ conferência visual" in fill.detail and "Empresa inválida" in fill.detail


def test_valor_longo_vai_e_volta_so_pelo_comeco():
    """Sessão 69020f7a: a visão ecoou 600 caracteres e o JSON chegou cortado."""
    longo = "Desenvolvimento full stack ponta a ponta de um produto de fidelidade para postos de combustível: " * 6
    ctx = {"campo": "Descrição", "valor": longo}
    q = c.pergunta("G2", ctx)
    assert "texto longo: confira só o começo" in q and longo not in q
    assert "as primeiras 80 letras" in q
    visto = longo[:80]
    assert c.interpretar("G2", ctx, json.dumps({"visto": visto, "erro_visivel": None})).ok is True
    assert c.interpretar("G2", ctx, json.dumps({"visto": "Texto antigo que não é o digitado de jeito nenhum", "erro_visivel": None})).ok is False
    g3 = c.pergunta("G3", {"botao": "SALVAR", "campos": [{"rotulo": "Descrição", "valor": longo}]})
    assert longo not in g3 and "até 80 letras do começo" in g3
