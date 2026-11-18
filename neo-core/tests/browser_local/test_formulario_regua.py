"""Formulário no navegador local: régua, mensagem, Enter e autocomplete.

O job 9fcb3bb6 (sessão 4a345d50, demoqa practice form) morreu no passo 18 com
8 de 10 campos certos: preencher não muda o TEXTO da tela, então a régua de
produção via "nada acrescentado". Junto vieram três defeitos que prepararam a
parada — o Enter que escolhia a sugestão foi tratado como envio, o texto
digitado no autocomplete passou por valor escolhido, e a mensagem do Daniel
chegou 200 ms antes da parada e se perdeu. Ver docs/browser-formulario-regua-plan.md.
"""

from __future__ import annotations

import copy
import json

import pytest

from core.browser_local import driver as driver_mod
from core.browser_local.conversa import Mensagem
from core.browser_local.driver import BrowserLocalDriver, NeedsConfirmation, Step


class _Resposta:
    def __init__(self, content: str) -> None:
        self._content = content

    def get_content(self) -> str:
        return self._content


class _Provider:
    def __init__(self, steps: list[dict]) -> None:
        self.steps = list(steps)

    async def complete(self, **kwargs):
        passo = self.steps.pop(0) if self.steps else {"action": "scroll", "args": {"delta": 300}}
        return _Resposta(json.dumps(passo))


class _LLM:
    def __init__(self, provider) -> None:
        self.provider = provider

    def get_provider_for_model(self, model: str):
        return self.provider


def _formulario(n: int) -> dict:
    """Uma página com N campos rotulados; o texto da tela NUNCA muda."""
    elements = []
    for i in range(n):
        elements.append({"ref": 2 * i, "tag": "label", "text": f"Pergunta de teste {i + 1}"})
        elements.append({"ref": 2 * i + 1, "tag": "input", "type": "text", "text": f"Resposta {i + 1}", "value": ""})
    return {
        "url": "https://form.exemplo/cadastro",
        "title": "Formulário",
        "ready": "complete",
        "stop": False,
        "elements": elements,
        "text": "Formulário de teste",
        "tela": "Formulário de teste com várias perguntas",
    }


def _driver_de_formulario(steps, pagina: dict, *, mensagens_na_rodada: dict | None = None, **kwargs):
    """O device de mentira: `fill` grava o valor no campo, e o snapshot mostra."""
    estado = copy.deepcopy(pagina)
    rodada = {"n": 0}

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 7}
        if action == "browser.snapshot":
            rodada["n"] += 1
            snap = copy.deepcopy(estado)
            if mensagens_na_rodada and rodada["n"] in mensagens_na_rodada:
                snap["mensagens"] = [mensagens_na_rodada[rodada["n"]]]
            return snap
        if action == "browser.fill":
            for el in estado["elements"]:
                if el.get("ref") == args.get("ref"):
                    el["value"] = args.get("value")
            return {"ok": True}
        if action == "browser.extract":
            return {"ok": True, "text": ""}
        return {"ok": True}

    provider = _Provider(steps)
    driver = BrowserLocalDriver(act=act, llm=_LLM(provider), model="texto", vision_model=None, **kwargs)
    return driver


# ── F1: campo novo é produção ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_formulario_longo_sem_palavra_nova_chega_ao_done():
    """O 9fcb3bb6: uma nota só no começo, depois um campo por passo."""
    n = 16
    passos = [{"action": "note", "args": {"texto": "Formulário de teste aberto: 16 perguntas"}}]
    passos += [{"action": "fill", "args": {"ref": 2 * i + 1, "value": f"valor {i + 1}"}} for i in range(n)]
    passos += [{"action": "done", "args": {"answer": "preenchi as 16"}}]
    driver = _driver_de_formulario(passos, _formulario(n), max_steps=25)

    result = await driver.run("preencha o formulário de teste sem enviar")

    assert "sem acrescentar nada" not in result.summary, result.summary[:300]
    assert result.status == "completed"
    assert "Campos na tela no fim" in result.summary
    assert "Pergunta de teste 16: valor 16" in result.summary


@pytest.mark.asyncio
async def test_mesmo_campo_preenchido_de_novo_e_giro():
    """Preencher o MESMO campo rende uma vez só; o resto é giro e para."""
    passos = [{"action": "note", "args": {"texto": "Formulário de teste aberto: 1 pergunta"}}]
    passos += [{"action": "fill", "args": {"ref": 1, "value": f"tentativa {i}"}} for i in range(40)]
    driver = _driver_de_formulario(passos, _formulario(1), max_steps=40)

    result = await driver.run("preencha o formulário de teste")

    assert "sem acrescentar nada" in result.summary
    assert driver._acting_steps <= driver_mod._SEM_GANHO_MAX + 2
    # F5: o motivo conta o que foi tentado, e não inventa rolagem nem espera.
    assert "fill ×" in result.summary
    assert "Rolar a página" not in result.summary


def test_datepicker_com_valor_padrao_trocado_rende_uma_vez():
    driver = BrowserLocalDriver(act=None, llm=None, model="texto", vision_model=None)
    campo = lambda v: {"elements": [  # noqa: E731
        {"ref": 15, "tag": "label", "text": "Date of Birth"},
        {"ref": 16, "tag": "input", "type": "text", "text": v, "value": v},
    ]}
    assert driver._campo_novo(campo("18 Nov 2026")) is False  # primeiro estado
    driver._interagiu = True
    assert driver._campo_novo(campo("15 May 1995")) is True
    assert driver._campo_novo(campo("16 May 1995")) is False


def test_lista_aberta_e_painel_nao_contam_como_campo():
    estado = driver_mod._estado_dos_campos([
        {"ref": 17, "tag": "label", "text": "Subjects"},
        {"ref": 18, "tag": "input", "type": "text", "text": "", "value": "Computer Science", "aberto": True},
        {"ref": 19, "tag": "select", "text": "Jan Feb", "value": "4", "panel": "Choose Date"},
        {"ref": 7, "tag": "input", "type": "radio", "text": "Male", "checked": True},
        {"ref": 8, "tag": "label", "text": "Male", "checked": True},
        {"ref": 27, "tag": "input", "type": "file", "text": ""},
    ])
    assert estado == {"input(radio)|Male": ("", True)}


def test_radio_alternado_no_mesmo_campo_nao_rende_de_novo():
    driver = BrowserLocalDriver(act=None, llm=None, model="texto", vision_model=None)
    radios = lambda male: {"elements": [  # noqa: E731
        {"ref": 7, "tag": "input", "type": "radio", "text": "Male", "checked": male},
        {"ref": 9, "tag": "input", "type": "radio", "text": "Female", "checked": not male},
    ]}
    driver._campo_novo({"elements": [
        {"ref": 7, "tag": "input", "type": "radio", "text": "Male", "checked": False},
        {"ref": 9, "tag": "input", "type": "radio", "text": "Female", "checked": False},
    ]})
    driver._interagiu = True
    assert driver._campo_novo(radios(True)) is True
    assert driver._campo_novo(radios(False)) is True   # Female, uma vez
    assert driver._campo_novo(radios(True)) is False
    assert driver._campo_novo(radios(False)) is False


# ── F2: mensagem do Daniel zera a régua e não se perde ──────────────────────


@pytest.mark.asyncio
async def test_mensagem_do_daniel_zera_a_regua():
    """Sem a mensagem, o giro para em ~12; com ela no meio, conta de novo dali."""
    passos = [{"action": "note", "args": {"texto": "Formulário de teste aberto: 1 pergunta"}}]
    passos += [{"action": "scroll", "args": {"delta": 200 + i * 10}} for i in range(60)]
    sem = _driver_de_formulario(list(passos), _formulario(1), max_steps=60)
    await sem.run("preencha o formulário de teste")
    parou_sem = sem._acting_steps

    com = _driver_de_formulario(
        list(passos), _formulario(1), max_steps=60,
        mensagens_na_rodada={10: "faltou selecionar o estado e a cidade"},
    )
    result = await com.run("preencha o formulário de teste")

    assert "sem acrescentar nada" in result.summary
    assert com._acting_steps >= parou_sem + 8, (parou_sem, com._acting_steps)


def test_mensagem_nao_vista_vai_no_resumo():
    driver = BrowserLocalDriver(act=None, llm=None, model="texto", vision_model=None)
    vista = Mensagem(texto="troca o gênero", origem="painel")
    nova = Mensagem(texto="faltou seleciona o state e uma cidade", origem="painel")
    driver.mensagens_do_daniel = [vista, nova]
    driver._mensagens_novas = {nova.id}

    resumo = driver._com_campos_e_mensagens("Parei: 12 passos seguidos sem acrescentar nada")

    assert "NÃO foi atendida" in resumo
    assert "faltou seleciona o state e uma cidade" in resumo
    assert "troca o gênero" not in resumo


# ── F3: Enter com lista de sugestões aberta não é envio ─────────────────────


def _pagina_com_submit(*, lista_aberta: bool) -> dict:
    elements = [
        {"ref": 18, "tag": "input", "type": "text", "text": "", "value": "Computer Science"},
        {"ref": 33, "tag": "button", "type": "submit", "text": "Submit"},
    ]
    if lista_aberta:
        elements.insert(1, {"ref": 19, "tag": "div", "type": "option", "text": "Computer Science"})
    return {"elements": elements}


def _driver_com_campos_do_neo() -> BrowserLocalDriver:
    driver = BrowserLocalDriver(act=None, llm=None, model="texto", vision_model=None)
    driver._preenchidos = [
        {"rotulo": "First Name", "valor": "Daniel", "origem": "neo", "chave": "", "aba": driver._ativa},
        {"rotulo": "Subjects", "valor": "Computer Science", "origem": "neo", "chave": "", "aba": driver._ativa},
    ]
    return driver


def test_enter_com_lista_de_sugestoes_aberta_nao_pede_conferencia():
    driver = _driver_com_campos_do_neo()
    driver._check_revisao_enter(_pagina_com_submit(lista_aberta=True))  # não levanta


def test_enter_sem_lista_aberta_continua_pedindo_conferencia():
    """A garantia da sessão e752a399 não pode cair."""
    driver = _driver_com_campos_do_neo()
    with pytest.raises(NeedsConfirmation):
        driver._check_revisao_enter(_pagina_com_submit(lista_aberta=False))


# ── F4: digitado no autocomplete ≠ escolhido ────────────────────────────────


def test_fill_que_abre_sugestoes_avisa_e_marca_pendente():
    driver = _driver_com_campos_do_neo()
    driver.steps = [Step("fill", {"ref": 18, "value": "Computer Science"}, True, "")]

    aviso = driver._aviso_de_sugestoes(_pagina_com_submit(lista_aberta=True))

    assert "[19]" in aviso and "LISTA DE SUGESTÕES" in aviso
    assert driver._preenchidos[-1].get("pendente") is True
    assert any("NÃO foi escolhida" in linha for linha in driver._linhas_de_revisao())


def test_fill_sem_sugestoes_nao_avisa():
    driver = _driver_com_campos_do_neo()
    driver.steps = [Step("fill", {"ref": 18, "value": "Computer Science"}, True, "")]
    assert driver._aviso_de_sugestoes(_pagina_com_submit(lista_aberta=False)) == ""
    assert "pendente" not in driver._preenchidos[-1]
