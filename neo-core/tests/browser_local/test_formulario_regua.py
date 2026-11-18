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


# ── F7: o que o E2E 7ce2a472 mostrou (sessão 15ccd81e) ──────────────────────


def _fim_do_7ce2a472() -> dict:
    """O último snapshot do job 7ce2a472, nos elementos que importam."""
    return {
        "tela": (
            "Student Registration Form\nName\nEmail\nGender Male Female Other\n"
            "Mobile (10 Digits)\nDate of Birth\nSubjects Maths Computer Science\n"
            "Hobbies Sports Reading Music\nPicture\nCurrent Address\nState and City NCR Delhi"
        ),
        "elements": [
            {"ref": 2, "tag": "label", "text": "Name"},
            {"ref": 3, "tag": "input", "type": "text", "text": "First Name", "value": "Teste"},
            {"ref": 4, "tag": "input", "type": "text", "text": "Last Name", "value": "Neo"},
            {"ref": 7, "tag": "input", "type": "radio", "text": "Male", "checked": True},
            {"ref": 8, "tag": "label", "text": "Male", "checked": True, "controla": "radio[gender]"},
            {"ref": 9, "tag": "input", "type": "radio", "text": "Female", "checked": False},
            {"ref": 10, "tag": "label", "text": "Female", "checked": False, "controla": "radio[gender]"},
            {"ref": 17, "tag": "label", "text": "Subjects"},
            {"ref": 18, "tag": "div", "type": "button", "text": "Remove Maths"},
            {"ref": 19, "tag": "div", "type": "button", "text": "Remove Computer Science"},
            {"ref": 20, "tag": "input", "type": "text", "text": "", "value": "", "box": [0, 0, 4, 24]},
            {"ref": 21, "tag": "label", "text": "Hobbies"},
            {"ref": 22, "tag": "input", "type": "checkbox", "text": "1", "checked": True},
            {"ref": 23, "tag": "label", "text": "Sports", "checked": True, "controla": "checkbox"},
            {"ref": 24, "tag": "input", "type": "checkbox", "text": "2", "checked": True},
            {"ref": 25, "tag": "label", "text": "Reading", "checked": True, "controla": "checkbox"},
            {"ref": 26, "tag": "input", "type": "checkbox", "text": "3", "checked": False},
            {"ref": 27, "tag": "label", "text": "Music", "checked": False, "controla": "checkbox"},
            {"ref": 30, "tag": "label", "text": "Current Address"},
            {"ref": 31, "tag": "textarea", "text": "Current Address", "value": "Rua de Teste, 123"},
            {"ref": 32, "tag": "label", "text": "State and City"},
            {"ref": 33, "tag": "button", "type": "submit", "text": "Submit"},
            {"ref": 34, "tag": "div", "text": "", "controla": "text"},
        ],
    }


def test_rotulos_dos_campos_do_7ce2a472():
    estado = driver_mod._estado_dos_campos(_fim_do_7ce2a472()["elements"])
    assert "input(checkbox)|Sports" in estado and "input(checkbox)|Reading" in estado
    assert "input(text)|Subjects" in estado, "os chips não podem esconder o rótulo"
    assert not any(k.endswith("|1") or k.endswith("|2") for k in estado)


def test_campos_sem_rotulo_nao_se_fundem():
    estado = driver_mod._estado_dos_campos([
        {"ref": 1, "tag": "button", "text": "Submit"},
        {"ref": 2, "tag": "input", "type": "text", "text": "", "value": "NCR"},
        {"ref": 3, "tag": "input", "type": "text", "text": "", "value": ""},
    ])
    assert len(estado) == 2


def test_bloco_do_fim_mostra_o_que_a_tela_mostra():
    driver = BrowserLocalDriver(act=None, llm=None, model="texto", vision_model=None)
    fim = _fim_do_7ce2a472()
    driver._campos_com_ganho = {"x", "y"}
    driver._estado_final_campos = driver_mod._estado_dos_campos(fim["elements"])
    driver._tela_final_campos = fim

    bloco = driver._bloco_campos_no_fim()

    assert "- Sports: marcado" in bloco and "- Reading: marcado" in bloco
    assert "- Male: marcado" in bloco and "Female" not in bloco
    assert "- Subjects: «Maths Computer Science»" in bloco
    assert "- State and City: «NCR Delhi»" in bloco
    assert "(sem rótulo)" not in bloco and "- 1: marcado" not in bloco
    assert "Hobbies" not in bloco  # o rótulo do grupo vem pelas caixas


@pytest.mark.asyncio
async def test_campos_vem_antes_do_caderno_e_o_caderno_avisa_que_envelhece():
    n = 3
    passos = [{"action": "note", "args": {"texto": "Pergunta de teste 3: pendente"}}]
    passos += [{"action": "fill", "args": {"ref": 2 * i + 1, "value": f"valor {i + 1}"}} for i in range(n)]
    passos += [{"action": "done", "args": {"answer": "preenchi"}}]
    driver = _driver_de_formulario(passos, _formulario(n), max_steps=25)

    result = await driver.run("preencha o formulário de teste sem enviar")

    assert result.summary.index("Campos na tela no fim") < result.summary.index("Anotações do caminho")
    assert "DESATUALIZADAS" in result.summary


def test_caixa_de_digitar_de_lista_ganha_dica():
    linha = BrowserLocalDriver._element_line(
        {"ref": 18, "tag": "input", "type": "text", "text": "", "box": [0, 0, 4, 24]}
    )
    assert "use `fill` direto" in linha
    normal = BrowserLocalDriver._element_line(
        {"ref": 3, "tag": "input", "type": "text", "text": "First Name", "box": [0, 0, 300, 38]}
    )
    assert "use `fill` direto" not in normal


# ── F9: o chat recebe os campos da tela, não o caderno velho ────────────────


def test_entrega_traz_os_campos_e_tira_as_notas_de_campo():
    from types import SimpleNamespace

    from core.jobs.runner_browser_local import _itens_da_entrega

    outcome = SimpleNamespace(
        notes=[
            "Subjects: pendente",
            "Hobbies: Sports e Music (Sports marcado; Music a marcar)",
            "Formulário completo: First Name Teste, Last Name Silva",
            "Notebook X — R$ 3.399 — loja Y",
        ],
        campos=["Name: Teste", "Last Name: Silva", "Subjects: «Maths» (texto na tela ao lado do rótulo)",
                "Sports: marcado", "Music: marcado"],
    )
    itens = _itens_da_entrega(outcome)

    assert itens[:5] == outcome.campos
    assert "Notebook X — R$ 3.399 — loja Y" in itens, "nota que não é de campo fica"
    assert not any("pendente" in i or "a marcar" in i for i in itens)


def test_entrega_sem_formulario_continua_sendo_o_caderno():
    from types import SimpleNamespace

    from core.jobs.runner_browser_local import _itens_da_entrega

    outcome = SimpleNamespace(notes=["a", "b"], campos=[])
    assert _itens_da_entrega(outcome) == ["a", "b"]


def test_um_campo_so_nao_e_formulario():
    """O link colado no site de download (c1a738d6) não vira bloco."""
    driver = BrowserLocalDriver(act=None, llm=None, model="texto", vision_model=None)
    driver._campos_com_ganho = {"input(text)|Paste your video link here"}
    driver._estado_final_campos = {"input(text)|Paste your video link here": ("https://y", None)}
    assert driver._bloco_campos_no_fim() == ""
    assert driver._linhas_campos_no_fim() == []


# ── F8a: dado pessoal que a memória não tem → pergunta viva ─────────────────


class _FillerSemData:
    async def tem_liberacao(self, **_):
        return False

    async def disponivel(self, **_):
        return "'data_nascimento' não está na memória"


def _driver_da_data(respostas):
    driver = BrowserLocalDriver(act=None, llm=None, model="texto", vision_model=None)
    driver._filler = _FillerSemData()
    driver._release_user_id = "u"
    driver._conversa = object()
    driver._last_seen_url = "https://demoqa.com/automation-practice-form"
    perguntas = []

    async def perguntar(pergunta, opcoes, **_):
        perguntas.append((pergunta, opcoes))
        if isinstance(respostas, Exception):
            raise respostas
        return respostas

    driver._perguntar = perguntar  # type: ignore[method-assign]
    driver._perguntas = perguntas  # type: ignore[attr-defined]
    return driver


_SNAP_DATA = {"elements": [
    {"ref": 15, "tag": "label", "text": "Date of Birth"},
    {"ref": 16, "tag": "input", "type": "text", "text": "18 Nov 2026", "value": "18 Nov 2026"},
]}


@pytest.mark.asyncio
async def test_data_ausente_pergunta_e_inventa_quando_ele_manda():
    driver = _driver_da_data([Mensagem(texto="pode inventar é so para teste", origem="chat")])
    args = {"ref": 16, "value": "10 Jan 1995"}

    assert await driver._barrar_literal(args, _SNAP_DATA, "10 Jan 1995") == args
    assert "não tenho esse dado seu na memória" in driver._perguntas[0][0]
    assert driver._perguntas[0][1] == ["Invente um valor de teste", "Deixe em branco"]
    # A decisão vale para o resto da tarefa: não pergunta de novo.
    assert await driver._barrar_literal(args, _SNAP_DATA, "11 Jan 1995") == args
    assert len(driver._perguntas) == 1
    assert driver._fills_barrados == []


@pytest.mark.asyncio
async def test_data_ausente_em_branco_pelo_botao():
    driver = _driver_da_data([Mensagem(texto="Deixe em branco", origem="painel", opcao=1)])
    with pytest.raises(RuntimeError, match="EM BRANCO"):
        await driver._barrar_literal({"ref": 16, "value": "10 Jan 1995"}, _SNAP_DATA, "10 Jan 1995")
    assert driver._fills_barrados == [], "deixar em branco não pode parar a tarefa"


@pytest.mark.asyncio
async def test_data_ausente_valor_ditado_por_ele_entra_e_so_ele():
    driver = _driver_da_data([Mensagem(texto="usa 05 Mar 1990", origem="chat")])
    with pytest.raises(RuntimeError, match="respondeu"):
        await driver._barrar_literal({"ref": 16, "value": "10 Jan 1995"}, _SNAP_DATA, "10 Jan 1995")
    ok = {"ref": 16, "value": "05 Mar 1990"}
    assert await driver._barrar_literal(ok, _SNAP_DATA, "05 Mar 1990") == ok
    with pytest.raises(RuntimeError, match="exatamente"):
        await driver._barrar_literal({"ref": 16, "value": "10 Jan 1995"}, _SNAP_DATA, "10 Jan 1995")


@pytest.mark.asyncio
async def test_data_ausente_sem_resposta_para_como_antes():
    from core.browser_local.driver import EsperaEsgotada, NeedsRelease

    driver = _driver_da_data(EsperaEsgotada("p", []))
    with pytest.raises(NeedsRelease):
        await driver._barrar_literal({"ref": 16, "value": "10 Jan 1995"}, _SNAP_DATA, "10 Jan 1995")


@pytest.mark.asyncio
async def test_dado_que_a_memoria_tem_continua_indo_para_o_card():
    """CPF/CEP que a memória TEM: o card de liberação, como sempre. Nada de inventar."""
    from core.browser_local.driver import NeedsRelease

    driver = _driver_da_data([Mensagem(texto="pode inventar", origem="chat")])

    async def tem(**_):
        return None

    driver._filler.disponivel = tem  # type: ignore[method-assign]
    with pytest.raises(NeedsRelease):
        await driver._barrar_literal({"ref": 16, "value": "10 Jan 1995"}, _SNAP_DATA, "10 Jan 1995")
    assert driver._perguntas == []


# ── F10: faixa de fundo não fecha a pergunta (sessão 119bde02) ──────────────


class _ConversaQueResponde:
    """Uma caixa de job: a resposta chega na 1ª espera; antes, a visão termina."""

    def __init__(self, durante_a_espera):
        self.pergunta = None
        self.ditas: list[str] = []
        self._durante = durante_a_espera
        self._chegou = False

    def tem_novas(self) -> bool:
        return self._chegou

    async def esperar(self, _s: float) -> bool:
        await self._durante()
        self._chegou = True
        return True

    def novas(self):
        return [Mensagem(texto="inventa", origem="painel")]

    def entregar(self, *a, **k):
        pass


@pytest.mark.asyncio
async def test_conferencia_que_termina_na_espera_nao_fecha_os_botoes():
    driver = BrowserLocalDriver(act=None, llm=None, model="texto", vision_model=None)
    faixas: list[tuple[str, str]] = []

    async def call(acao, args):
        if acao == "browser.banner":
            aberta = driver._conversa.pergunta is not None
            faixas.append((args["kind"], "COM a pergunta aberta" if aberta else "depois"))
        return {"ok": True}

    driver._call = call  # type: ignore[method-assign]

    async def visao_termina():
        await driver._banner("👁 conferido: o campo mostra Souza", kind="visto")

    driver._conversa = _ConversaQueResponde(visao_termina)

    respostas = await driver._perguntar("A página pede data de nascimento…", ["Invente", "Deixe em branco"])

    assert [m.texto for m in respostas] == ["inventa"]
    kinds = [k for k, _ in faixas]
    assert kinds[0] == "pergunta"
    # Nada que não seja pergunta entre a pergunta e a resposta: é o que o
    # desktop lê como "a pergunta acabou" e fecha os botões.
    assert ("visto", "depois") in faixas, faixas
    assert ("visto", "COM a pergunta aberta") not in faixas, faixas
    assert driver._pergunta_aberta is False
    assert driver._faixas_seguradas == []


@pytest.mark.asyncio
async def test_pergunta_do_dado_ausente_cita_o_rotulo_e_nao_o_valor():
    driver = _driver_da_data([Mensagem(texto="inventa", origem="painel")])
    await driver._barrar_literal({"ref": 16, "value": "10 Jan 1995"}, _SNAP_DATA, "10 Jan 1995")
    assert "«Date of Birth»" in driver._perguntas[0][0]
    assert "18 Nov 2026" not in driver._perguntas[0][0]


def test_nota_do_mobile_sai_quando_o_rotulo_tem_parentese():
    from types import SimpleNamespace

    from core.jobs.runner_browser_local import _itens_da_entrega

    outcome = SimpleNamespace(notes=["Mobile: 11987654321"], campos=["Mobile(10 Digits): 11987654321", "Name: X"])
    assert _itens_da_entrega(outcome) == outcome.campos
