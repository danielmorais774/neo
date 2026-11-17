"""Prints de confirmação (docs/browser-visao-confirmacao-plan.md, fase dos gatilhos)."""

from __future__ import annotations

import base64

import pytest

import shared.ajustes as ajustes_mod
from core.browser_local import visao
from core.browser_local.driver import _ENVIO
from core.browser_local.visao import EstadoDaVisao, antes_da_acao, depois_da_acao, juntar

FORM = "https://www.geekhunter.com/candidates/preview"


def _snap(*elementos, url=FORM):
    return {"url": url, "title": "GeekHunter", "elements": list(elementos)}


EMPRESA = {"ref": 3, "tag": "input", "text": "Empresa", "box": [100, 200, 400, 40]}
CAIXA = {"ref": 7, "tag": "label", "text": "Trabalho aqui atualmente", "checked": True, "box": [100, 300, 200, 24]}
SALVAR = {"ref": 15, "tag": "button", "text": "SALVAR", "box": [700, 600, 80, 32]}


def _antes(estado, action, args, snap, fp="t1", painel=None):
    return antes_da_acao(estado, action, args, snap, fingerprint=fp, painel=painel, envio=_ENVIO)


def test_primeiro_campo_do_formulario_tira_g1_e_o_seguinte_so_se_a_tela_mudou():
    e = EstadoDaVisao()
    assert [d.gatilho for d in _antes(e, "fill", {"ref": 3, "value": "Acme"}, _snap(EMPRESA))] == ["G1"]
    e.ultimo_inteiro = "t1"
    # mesma tela do último print inteiro: o "antes" do campo seguinte é o "depois" do anterior
    assert _antes(e, "click", {"ref": 7}, _snap(EMPRESA, CAIXA), fp="t1") == []
    assert [d.gatilho for d in _antes(e, "click", {"ref": 7}, _snap(EMPRESA, CAIXA), fp="t2")] == ["G1b"]
    # outro painel = outro formulário
    assert [d.gatilho for d in _antes(e, "fill", {"ref": 3}, _snap(EMPRESA), fp="t2", painel="modal")] == ["G1"]


def test_clique_comum_nao_e_campo_e_salvar_e_envio():
    e = EstadoDaVisao()
    assert _antes(e, "click", {"ref": 99}, _snap({"ref": 99, "text": "edit"})) == []
    assert [d.gatilho for d in _antes(e, "click", {"ref": 15}, _snap(SALVAR))] == ["G3"]
    assert e.pendente["envio"] == "SALVAR"
    assert [d.gatilho for d in _antes(EstadoDaVisao(), "click_text", {"texto": "Enviar"}, _snap())] == ["G3"]


def test_depois_do_campo_e_recortado_no_campo_com_a_faixa_de_erro():
    e = EstadoDaVisao()
    _antes(e, "fill", {"ref": 3, "value": "Acme"}, _snap(EMPRESA))
    movido = {**EMPRESA, "box": [100, 260, 400, 40]}  # a página refluiu
    [d] = depois_da_acao(e, _snap(movido), painel_novo=None, margem=120)
    assert d.gatilho == "G2" and "Empresa" in d.motivo
    assert d.recorte == [76.0, 220.0, 448.0, 200.0]
    assert e.pendente is None


def test_depois_de_enviar_que_navega_vira_um_print_so():
    e = EstadoDaVisao()
    _antes(e, "click", {"ref": 15}, _snap(SALVAR))
    disparos = depois_da_acao(e, _snap(url=FORM + "/ok"), painel_novo=None, margem=120)
    assert [d.gatilho for d in disparos] == ["G4", "G7"]
    [um] = juntar(disparos, fingerprint="t9", estado=e)
    assert um.gatilho == "G4+G7" and um.recorte is None


def test_painel_aberto_e_upload():
    e = EstadoDaVisao()
    _antes(e, "click", {"ref": 99}, _snap({"ref": 99, "text": "edit"}))
    [d] = depois_da_acao(e, _snap(), painel_novo={"rotulo": "Foto", "box": [10, 10, 500, 300]}, margem=120)
    assert d.gatilho == "G5" and d.recorte == [10.0, 10.0, 500.0, 300.0]
    _antes(e, "upload", {"ref": 9, "arquivo": "x.png"}, _snap({"ref": 9, "text": "Foto"}))
    assert [d.gatilho for d in depois_da_acao(e, _snap(), painel_novo=None, margem=120)] == ["G8"]


def test_juntar_nao_repete_tela_inteira_mas_mantem_o_envio():
    e = EstadoDaVisao(ultimo_inteiro="t1")
    assert juntar([visao.Disparo("G1b", "x")], fingerprint="t1", estado=e) == []
    [g3] = juntar([visao.Disparo("G1b", "x"), visao.Disparo("G3", "antes de enviar")], fingerprint="t1", estado=e)
    assert g3.gatilho == "G3"


# ── driver de ponta a ponta, com o booleano ligado ─────────────────────────


@pytest.fixture
def visao_ligada(tmp_path, monkeypatch):
    arquivo = tmp_path / "neo.toml"
    arquivo.write_text(
        "[navegador.conversa]\nespera_min = 0.005\nespera_intervalo_s = 0.2\n"
        "[navegador.visao]\nconfirmacao_visual = true\nprints_max = 60\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NEO_AJUSTES", str(arquivo))
    monkeypatch.setattr(ajustes_mod, "_cache", None)
    yield


class _Formulario:
    def __init__(self):
        self.valor = ""
        self.salvo = False
        self.prints: list[dict] = []
        self.ordem: list[str] = []

    async def act(self, action, args, timeout):
        if action in ("browser.fill", "browser.snapshot", "browser.click", "browser.screenshot"):
            self.ordem.append(action.replace("browser.", "") + (":alvo" if args.get("alvo_ref") is not None else ""))
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            if self.salvo:
                return {"url": FORM + "/ok", "title": "Salvo", "ready": "complete", "stop": False,
                        "elements": [{"ref": 1, "tag": "div", "text": "Experiência salva"}], "text": "ok"}
            return {"url": FORM, "title": "Experiência", "ready": "complete", "stop": False,
                    "elements": [{**EMPRESA, "value": self.valor}, SALVAR], "text": "Empresa"}
        if action == "browser.fill":
            self.valor = args.get("value", "")
            return {"ok": True}
        if action == "browser.click":
            self.salvo = args.get("ref") == 15
            return {"ok": True, "label": "SALVAR"}
        if action == "browser.screenshot":
            self.prints.append(dict(args))
            return {"ok": True, "data": base64.b64encode(b"jpeg").decode(), "marks": False,
                    "clipped": "clip" in args, "tempos": {"total_ms": 300}}
        return {"ok": True}


@pytest.mark.asyncio
async def test_formulario_inteiro_tira_os_prints_de_confirmacao(visao_ligada):
    from tests.browser_local.test_conversa import _driver

    device = _Formulario()
    driver, _ = _driver(
        [
            {"action": "fill", "args": {"ref": 3, "value": "Acme"}},
            {"action": "click", "args": {"ref": 15}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        device,
        confirm_sensitive=False,
    )
    await driver.run("cadastrar a experiência", start_url=FORM)
    gatilhos = [p["gatilho"] for p in driver.prints_de_confirmacao]
    assert gatilhos == ["G1", "G2", "G3", "G4+G7"]
    # nenhum print de confirmação desenha números; o do campo sai recortado
    assert all(p.get("marks") is False for p in device.prints)
    # o do campo é guiado pelo NÚMERO (o desktop mede o campo na hora)…
    assert device.prints[1].get("alvo_ref") == 3 and "alvo_ref" not in device.prints[0]
    # …e sai logo depois do fill, ANTES do snapshot seguinte (antes de rolar)
    i = device.ordem.index("fill")
    assert device.ordem[i + 1] == "screenshot:alvo" and device.ordem[i + 2] == "snapshot"


@pytest.mark.asyncio
async def test_desligado_nao_tira_print_nenhum_a_mais(tmp_path, monkeypatch):
    from tests.browser_local.test_conversa import _driver

    arquivo = tmp_path / "neo.toml"
    arquivo.write_text("[navegador.visao]\nconfirmacao_visual = false\n", encoding="utf-8")
    monkeypatch.setenv("NEO_AJUSTES", str(arquivo))
    monkeypatch.setattr(ajustes_mod, "_cache", None)
    device = _Formulario()
    driver, _ = _driver(
        [{"action": "fill", "args": {"ref": 3, "value": "Acme"}},
         {"action": "click", "args": {"ref": 15}},
         {"action": "done", "args": {"answer": "ok"}}],
        device,
        confirm_sensitive=False,
    )
    await driver.run("cadastrar", start_url=FORM)
    assert driver.prints_de_confirmacao == []
    # só o print do fim (para o chat), como sempre
    assert len(device.prints) <= 1


def test_diario_nomeia_o_print_pelo_gatilho_sem_sobrescrever(tmp_path):
    from core.browser_local.trace import RunTrace

    trace = RunTrace(tmp_path, "job-v")
    dado = base64.b64encode(b"jpeg").decode()
    assert trace.shot(5, dado, False, gatilho="G2 — depois de «Empresa»", recorte=True) == "05-G2.jpg"
    assert trace.shot(5, dado, False, gatilho="G2 — depois de «Cargo»", recorte=True) == "05-G2-2.jpg"
    assert trace.shot(6, dado, False, gatilho="G4+G7 — depois de enviar") == "06-G4-G7.jpg"
    texto = (trace.dir / "trace.md").read_text(encoding="utf-8")
    assert "confirmação G2 — depois de «Empresa» (recortado)" in texto


def test_campo_renumerado_depois_do_fill_e_achado_pela_identidade():
    """Sessão 69020f7a: a descrição era [28] antes do fill e [18] depois; o [28]
    virou o seletor de mês e o print recortava o elemento errado."""
    descricao = {"ref": 28, "tag": "textarea", "text": "Escreva uma descrição", "value": "Desenvolvimento full", "box": [100, 900, 990, 122]}
    e = EstadoDaVisao()
    _antes(e, "fill", {"ref": 28, "value": "Desenvolvimento full stack, Referência técnica do time de produto"}, _snap(descricao))
    depois = _snap(
        {"ref": 18, "tag": "textarea", "text": "Escreva uma descrição", "value": "Desenvolvimento full stack, Refer", "box": [100, 300, 990, 122]},
        {"ref": 28, "tag": "select", "text": "Mês Janeiro", "box": [100, 500, 120, 40]},
    )
    [d] = depois_da_acao(e, depois, painel_novo=None, margem=120)
    assert d.recorte == visao.recorte_do_campo([100.0, 300.0, 990.0, 122.0], 120)
    # sem achar o campo, nada de caixa velha: print inteiro
    _antes(e, "fill", {"ref": 28, "value": "x"}, _snap(descricao))
    [d2] = depois_da_acao(e, _snap({"ref": 28, "tag": "select", "text": "Mês"}), painel_novo=None, margem=120)
    assert d2.recorte is None


def test_logo_depois_guia_pelo_numero_e_le_a_caixa_pelo_retorno():
    e = EstadoDaVisao()
    _antes(e, "click", {"ref": 7}, _snap(CAIXA))
    d = visao.logo_depois(e, "Trabalho aqui atualmente → DESMARCADO (estava marcado; o clique pegou)")
    assert d.alvo_ref == 7 and d.contexto["marcado"] is False and d.recorte is None
    # já fotografado: o snapshot seguinte não repete o G2
    assert [x.gatilho for x in depois_da_acao(e, _snap(CAIXA), painel_novo=None, margem=120)] == []
    # sem número (click_text), fica para o snapshot seguinte
    e2 = EstadoDaVisao()
    _antes(e2, "fill", {"ref": None, "value": "x"}, _snap(EMPRESA))
    e2.pendente["ref"] = None
    assert visao.logo_depois(e2, "ok") is None
    # o recorte guiado conta como recorte no `juntar`, não como tela inteira
    [j] = juntar([d], fingerprint="t1", estado=EstadoDaVisao(ultimo_inteiro="t1"))
    assert j.alvo_ref == 7
