"""O formulário é respondido pela memória, pergunta por pergunta, e conferido antes de ir.

O caso (sessão e752a399, 2026-11-01): o Daniel pediu "preencha e envie" num
Google Forms que perguntava o treino de cada dia da semana, a academia e o
objetivo. O cérebro escreveu as oito respostas dentro da tarefa, com o bloco de
memória do turno — ranqueado por "preencha e envie", que trouxe só o treino de
antebraço. Segunda, quarta, quinta e sexta saíram inventados ("Desenvolvimento
militar", "Stiff", "Mesa flexora", séries que não existem), e o "Enviar" passou
direto. Perguntado só pelo treino de sexta, o mesmo Neo acertava.

A evidência daqui é o texto que o ranking v2 devolveu em produção para os oito
rótulos reais (medido em 2026-11-01, só leitura), recortado.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.browser_local.driver import BrowserLocalDriver
from core.browser_local.respondedor import (
    Resposta,
    Respondedor,
    agrupar,
    aterrar,
    chave_da_pergunta,
    respostas_de,
)

from tests.browser_local.test_driver_vision import _FakeLLM, _FakeProvider

PERGUNTAS = [
    "seu email",
    "Coloque abaixo o treino de segunda-feira com exercícios e repetições",
    "Coloque abaixo o treino de terça-feira com exercícios e repetições",
    "Coloque abaixo o treino de quarta-feira com exercícios e repetições",
    "Coloque abaixo o treino de quinta-feira com exercícios e repetições",
    "Coloque abaixo o treino de sexta-feira com exercícios e repetições",
    "Nome da academia que você frequenta?",
    "Seu objetivo (s) — opções: Hipertrofia, Cutting, Força, Saude, Aliviar estresse",
]

DONO = """### Daniel Morais [person] — o próprio Daniel, dono dos dados
- email: «pks:email»
- full_name: Daniel Morais
- cpf: «pks:cpf»
- belongs_to -> Academia Exemplo [place]; Objetivo do treino de Daniel [goal]
- works_on -> Plano de treino de academia [routine]"""

SEGUNDA = """### Treino de segunda-feira [workout]
- label: Peito e Tríceps
- weekday: monday
- has_exercise: Crucifixo; Tríceps na polia; Supino Reto; Tríceps francês crossover (reps: 15, sets: 4, weight_note: peso menor não especificado); Supino Inclinado; Tríceps costas na polia alta
- has_member <- Plano de treino de academia"""
TERCA = """### Treino de terça-feira [workout]
- label: Costas e Bíceps
- weekday: tuesday
- has_exercise: Remada Alta com Barra; Remada Sentado na Polia; Rosca Alternada com Halteres; Remada na Máquina Articulada Unilateral; Rosca Sentado no Banco com as Duas Mãos; Rosca em Pé"""
ANTEBRACO = """### Treino de antebraço e punho [workout]
- label: Antebraço e Punho
- weekday: tuesday
- has_exercise: Farmer's walk (reps: até a falha, sets: 3); Rosca inversa (reps: 10-12, sets: 3); Rosca de punho invertida (reps: 12-15, sets: 3); Rosca de punho com barra (reps: 12-15, sets: 3); Aperto de mão no hand grip (reps: até a falha, sets: 3)"""
QUARTA = """### Treino de quarta-feira [workout]
- label: Perna
- weekday: wednesday
- has_exercise: Leg Press; Cadeira Extensora; Cadeira Flexora; Panturrilha em Pé; Cadeira Abdutora; Leg Press Sentado; Agachamento na Máquina em Pé"""
QUINTA = """### Treino de quinta-feira [workout]
- label: Ombro
- weekday: thursday
- has_exercise: Desenvolvimento na Máquina; Elevação Frontal; Elevação Lateral; Travésio com Halteres; Polia Unilateral de Ombro; Puxada Baixa na Polia"""
SEXTA = """### Treino de sexta-feira [workout]
- label: Peito e Bíceps
- weekday: friday
- has_exercise: Supino Reto; Supino Inclinado; Rosca em Pé; Martelo com Halteres; Crucifixo na Máquina; Rosca Scott"""
PLANO = """### Plano de treino de academia [routine]
- frequency_per_week: 5
- routine_type: split"""
OBJETIVO = """### Objetivo do treino de Daniel [goal]
- também chamado: força; hipertrofia; hipertrofia e força
- related_to -> Plano de treino de academia; Academia Exemplo"""

_TREINOS = [("seg", SEGUNDA), ("ter", TERCA), ("ant", ANTEBRACO), ("qua", QUARTA),
            ("qui", QUINTA), ("sex", SEXTA), ("plano", PLANO)]


def _primeiro(chave: str) -> list:
    """O do dia em primeiro, como o ranking devolveu (nota 0.63 contra 0.28)."""
    return sorted(_TREINOS, key=lambda kv: kv[0] != chave)


#: O que `evidencia_de_formulario` devolveu para os 8 rótulos, na forma.
EVIDENCIA = {
    "dono": DONO,
    "perguntas": [
        [],  # "seu email": a busca não acha campo do dono — o bloco dele responde
        _primeiro("seg"), _primeiro("ter"), _primeiro("qua"), _primeiro("qui"), _primeiro("sex"),
        [("plano", PLANO)],  # academia: a busca não acha "Academia Exemplo" (sem campo)
        [("obj", OBJETIVO)],
    ],
}


class _Evidencia:
    def __init__(self, dados=EVIDENCIA):
        self.dados = dados
        self.chamadas: list[list[str]] = []

    async def __call__(self, perguntas):
        self.chamadas.append(list(perguntas))
        return self.dados


class _LLM:
    def __init__(self, respostas=None, *, boom=None):
        self.respostas = respostas
        self.boom = boom
        self.chamadas: list[dict] = []

    async def complete(self, system, user, *, max_tokens):
        self.chamadas.append({"system": system, "user": user, "max_tokens": max_tokens})
        if self.boom:
            raise self.boom
        return json.dumps({"respostas": self.respostas})


#: O que um modelo plausível devolve — com os MESMOS erros do turno real
#: misturados no meio, para o aterramento ter o que cortar.
RESPOSTAS_DO_MODELO = [
    {"n": 1, "resposta": "«pks:email»", "fontes": ["Daniel Morais"]},
    {"n": 2, "resposta": "Supino Reto; Supino Inclinado; Crucifixo; Tríceps na polia; "
                         "Tríceps costas na polia alta; Tríceps francês crossover 4x15; "
                         "Desenvolvimento militar 4x8",
     "fontes": ["Treino de segunda-feira [workout]"], "faltou": "séries de 5 dos 6 exercícios"},
    {"n": 3, "resposta": "Remada Alta com Barra; Rosca em Pé; Rosca de punho com barra 3x12-15",
     "fontes": ["Treino de terça-feira", "Treino de antebraço e punho"],
     "observacao": "dois treinos marcados para terça"},
    {"n": 4, "resposta": "Agachamento livre 4x8; Leg Press; Cadeira Extensora; Stiff 3x10",
     "fontes": ["Treino de quarta-feira"]},
    {"n": 5, "resposta": "Desenvolvimento na Máquina; Elevação Frontal; Elevação Lateral",
     "fontes": ["Treino de quinta-feira"]},
    {"n": 6, "resposta": "Supino Reto; Supino Inclinado; Crucifixo na Máquina; Rosca em Pé; "
                         "Rosca Scott; Martelo com Halteres",
     "fontes": ["Treino de sexta-feira"]},
    {"n": 7, "resposta": "Academia Exemplo", "fontes": ["Daniel Morais (você)"]},
    {"n": 8, "resposta": "Hipertrofia; Força; Saude", "fontes": ["Objetivo do treino de Daniel"]},
]


# ── 1. respondedor: agrupa, pergunta ao LLM, e corta o que não tem lastro ─────


@pytest.mark.asyncio
async def test_oito_perguntas_viram_uma_chamada_e_cada_dia_sai_da_memoria():
    llm = _LLM(RESPOSTAS_DO_MODELO)
    ev = _Evidencia()
    respostas = await Respondedor(evidencia=ev, llm=llm, finalidade="preencha e envie").responder(PERGUNTAS)

    # A busca é UMA POR PERGUNTA (com as 8 de uma vez); o LLM, uma vez só.
    assert ev.chamadas == [PERGUNTAS]
    assert len(llm.chamadas) == 1
    prompt = llm.chamadas[0]["user"]
    for p in PERGUNTAS:
        assert f"«{p}»" in prompt
    assert "### Daniel Morais [person]" in prompt and "preencha e envie" in prompt
    # A dica de "o que a busca achou primeiro" é a do dia perguntado.
    assert "a busca achou primeiro: Treino de quarta-feira" in prompt

    por = {r.pergunta: r for r in respostas}
    assert [r.pergunta for r in respostas] == PERGUNTAS
    assert por[PERGUNTAS[0]].resposta == "«pks:email»"
    segunda = por[PERGUNTAS[1]]
    assert "Tríceps francês crossover 4x15" in segunda.resposta
    assert "Desenvolvimento militar" not in segunda.resposta
    assert "Desenvolvimento militar" in segunda.observacao
    assert segunda.fontes == ["Treino de segunda-feira"]
    assert segunda.faltou == "séries de 5 dos 6 exercícios"
    terca = por[PERGUNTAS[2]]
    assert "Rosca de punho com barra 3x12-15" in terca.resposta
    assert terca.fontes == ["Treino de terça-feira", "Treino de antebraço e punho"]
    assert "dois treinos" in terca.observacao
    quarta = por[PERGUNTAS[3]]
    assert quarta.resposta == "Leg Press; Cadeira Extensora"
    assert "Agachamento livre" in quarta.observacao and "Stiff" in quarta.observacao
    assert por[PERGUNTAS[5]].resposta.count(";") == 5
    assert por[PERGUNTAS[6]].resposta == "Academia Exemplo"
    assert por[PERGUNTAS[6]].fontes == ["Daniel Morais"]
    # "Saude" não está na memória; "Força" está, nos aliases do objetivo.
    assert por[PERGUNTAS[7]].resposta == "Hipertrofia; Força"


@pytest.mark.asyncio
async def test_falha_do_llm_deixa_tudo_em_branco_e_nunca_chuta():
    respostas = await Respondedor(
        evidencia=_Evidencia(), llm=_LLM(boom=RuntimeError("402 Payment Required"))
    ).responder(PERGUNTAS[:3])
    assert [r.resposta for r in respostas] == [None, None, None]
    assert all("falhou" in r.faltou for r in respostas)


@pytest.mark.asyncio
async def test_pergunta_repetida_e_consultada_uma_vez():
    ev = _Evidencia({"dono": DONO, "perguntas": [[("seg", SEGUNDA)]]})
    llm = _LLM([{"n": 1, "resposta": "Supino Reto", "fontes": ["Treino de segunda-feira"]}])
    respostas = await Respondedor(evidencia=ev, llm=llm).responder(
        ["Treino de segunda *", "treino de SEGUNDA", "  Treino   de segunda * "]
    )
    assert ev.chamadas == [["Treino de segunda *"]]
    assert len(respostas) == 1


def test_agrupar_junta_quem_divide_evidencia_e_empacota_o_resto():
    texto = "x" * 400  # ~100 tokens
    listas = [[("plano", texto)], [("plano", texto)], [("obj", texto)], []]
    assert agrupar(listas, 6000) == [[0, 1, 2, 3]]
    # Sem orçamento para tudo: o componente conexo fica junto, o resto à parte.
    assert agrupar(listas, 150) == [[0, 1, 3], [2]]


def test_agrupar_fatia_componente_maior_que_o_orcamento():
    grande = "y" * 2000  # ~500 tokens cada
    listas = [[("a", grande), ("b", grande)], [("b", grande), ("c", grande)], [("c", grande)]]
    lotes = agrupar(listas, 1100)
    assert sorted(i for lote in lotes for i in lote) == [0, 1, 2]
    assert len(lotes) > 1


def test_aterrar_corta_fonte_e_marcador_que_nao_existem():
    memoria = DONO + "\n" + SEXTA
    sem_fonte = aterrar("treino de sexta", {"resposta": "Supino Reto", "fontes": ["Plano B"]}, memoria)
    assert sem_fonte.resposta is None and "não disse de onde" in sem_fonte.observacao
    marcador = aterrar("telefone", {"resposta": "«pks:telefone»", "fontes": ["Daniel Morais"]}, memoria)
    assert marcador.resposta is None and "«pks:telefone»" in marcador.observacao
    numero = aterrar(
        "treino de sexta", {"resposta": "Supino Reto 4x8; Rosca Scott", "fontes": ["Treino de sexta-feira"]},
        memoria,
    )
    # "4x8": nenhum desses números está na memória da sexta.
    assert numero.resposta == "Rosca Scott"


def test_resposta_vai_e_volta_pela_sessao_da_aba():
    r = Resposta("seu email", "«pks:email»", ["Daniel Morais"], "", "")
    assert respostas_de([r.to_dict(), {"pergunta": ""}, "lixo"]) == [r]
    assert chave_da_pergunta("Nome da Academia? *") == chave_da_pergunta("nome da academia?")


# ── 2. o job: consulta, preenche, e para antes de enviar ─────────────────────

URL = "https://docs.google.com/forms/d/e/1FAIpQLSe-UO/viewform"


def _form() -> dict:
    return {
        "url": URL,
        "title": "Informe o seu treino",
        "ready": "complete",
        "stop": False,
        "elements": [
            {"ref": 9, "tag": "input", "type": "text", "text": "seu email"},
            {"ref": 14, "tag": "textarea", "type": "textarea", "text": PERGUNTAS[1]},
            {"ref": 34, "tag": "input", "type": "text", "text": PERGUNTAS[6]},
            {"ref": 35, "tag": "div", "type": "checkbox", "text": "Hipertrofia"},
            {"ref": 36, "tag": "div", "type": "button", "text": "Enviar"},
            {"ref": 37, "tag": "div", "type": "button", "text": "Próxima"},
        ],
        "text": "Informe o seu treino seu email " + " ".join(PERGUNTAS) + " Enviar",
    }


class _RespondedorFake:
    def __init__(self, respostas):
        self.respostas = respostas
        self.chamadas: list[list[str]] = []

    async def responder(self, perguntas):
        self.chamadas.append(list(perguntas))
        por = {chave_da_pergunta(r.pergunta): r for r in self.respostas}
        return [por.get(chave_da_pergunta(p)) or Resposta(p, faltou="a memória não tem") for p in perguntas]


DA_MEMORIA = [
    Resposta(PERGUNTAS[1], "Supino Reto; Supino Inclinado; Crucifixo", ["Treino de segunda-feira"],
             "séries de 2 dos 3 exercícios"),
    Resposta(PERGUNTAS[6], "Academia Exemplo", ["Daniel Morais"]),
    Resposta(PERGUNTAS[7], "Hipertrofia", ["Objetivo do treino de Daniel"]),
    Resposta(PERGUNTAS[3], None, [], "a memória não tem o treino de quarta"),
]


def _driver(steps, *, acted, respondedor=None, previas=None, liberado="", pedido="preencha e envie",
            filler=None):
    digitado: dict = {}

    def _tela() -> dict:
        tela = _form()
        for item in tela["elements"]:
            if item["ref"] in digitado:
                item["value"] = digitado[item["ref"]]
        return tela

    async def act(action, args, timeout):
        acted.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.fill":
            digitado[args.get("ref")] = args.get("value")
            return {"ok": True}
        if action == "browser.snapshot":
            return _tela()
        if action == "browser.screenshot":
            return {"ok": True, "format": "jpeg", "data": "UFJJTlQ=", "marks": True}
        if action == "browser.downloads":
            return {"ok": True, "dir": "/Users/daniel/Downloads", "count": 0, "files": []}
        return {"ok": True}

    provider = _FakeProvider(steps)
    driver = BrowserLocalDriver(
        act=act,
        llm=_FakeLLM({}, provider),
        model="texto",
        user_id=uuid4(),
        respondedor=respondedor,
        respostas_previas=previas,
        allow_sensitive_label=liberado,
        pedido=pedido,
        filler=filler,
    )
    return driver, provider


def _cliques(acted):
    return [a for a in acted if a[0] in ("browser.click", "browser.click_text")]


@pytest.mark.asyncio
async def test_consulta_preenche_e_para_antes_de_enviar_com_o_que_vai_e_de_onde_veio():
    acted: list = []
    resp = _RespondedorFake(DA_MEMORIA)
    driver, provider = _driver(
        [
            {"action": "consultar", "args": {"perguntas": [PERGUNTAS[1], PERGUNTAS[3], PERGUNTAS[6], PERGUNTAS[7]]}},
            {"action": "fill", "args": {"ref": 14, "value": "Supino Reto; Supino Inclinado; Crucifixo"}},
            {"action": "fill", "args": {"ref": 34, "value": "Academia Exemplo"}},
            {"action": "click_text", "args": {"texto": "Hipertrofia"}},
            {"action": "click", "args": {"ref": 36}},
        ],
        acted=acted, respondedor=resp,
    )
    out = await driver.run("Preencha o formulário respondendo cada pergunta pela memória e envie", start_url=URL)

    assert out.status == "needs_confirmation", out.summary
    assert out.pending == "Enviar"
    assert "confira o que vai em cada campo" in out.summary
    # O "Enviar" NÃO foi clicado; o checkbox, sim.
    assert [a for a in _cliques(acted) if a[1].get("ref") == 36] == []
    assert len(resp.chamadas) == 1
    revisao = "\n".join(out.revisao)
    assert f"«{PERGUNTAS[1]}» → Supino Reto; Supino Inclinado; Crucifixo — da memória (Treino de segunda-feira)" in revisao
    assert "faltou: séries de 2 dos 3 exercícios" in revisao
    assert f"«{PERGUNTAS[6]}» → Academia Exemplo — da memória (Daniel Morais)" in revisao
    # Resposta de clique (checkbox) também aparece, e o que ficou em branco também.
    assert f"«{PERGUNTAS[7]}» → Hipertrofia — da memória" in revisao
    assert f"«{PERGUNTAS[3]}» → em branco: a memória não tem o treino de quarta" in revisao
    # E as respostas viajam com a parada, para a retomada.
    assert {r["pergunta"] for r in out.respostas} == {PERGUNTAS[1], PERGUNTAS[3], PERGUNTAS[6], PERGUNTAS[7]}
    # O quadro de respostas está no prompt dos passos seguintes.
    depois = provider.calls[1]["messages"][1].content
    assert "RESPOSTAS DA MEMÓRIA" in depois and "Academia Exemplo" in depois


@pytest.mark.asyncio
async def test_o_turno_real_texto_escrito_pelo_neo_tambem_para_antes_de_enviar():
    """e752a399 de novo, sem `consultar`: o cérebro escreveu o treino na tarefa."""
    acted: list = []
    inventado = "Supino reto 4x8; Desenvolvimento militar 4x8; Tríceps testa 3x10"
    driver, _ = _driver(
        [
            {"action": "fill", "args": {"ref": 14, "value": inventado}},
            {"action": "click", "args": {"ref": 36}},
        ],
        acted=acted,
    )
    out = await driver.run(f"Preencha o treino de segunda = '{inventado}' e clique em Enviar", start_url=URL)
    assert out.status == "needs_confirmation"
    assert out.revisao == [
        f"«{PERGUNTAS[1]}» → {inventado} — ⚠ escrito pelo Neo: não veio da sua memória nem do seu pedido"
    ]


@pytest.mark.asyncio
async def test_o_que_o_daniel_ditou_vai_sem_conferencia():
    acted: list = []
    driver, _ = _driver(
        [
            {"action": "fill", "args": {"ref": 34, "value": "Academia Exemplo"}},
            {"action": "click", "args": {"ref": 36}},
            {"action": "done", "args": {"answer": "enviado"}},
        ],
        acted=acted, pedido="põe Academia Exemplo no nome da academia e envia",
    )
    out = await driver.run("Preencha a academia com 'Academia Exemplo' e envie", start_url=URL)
    assert out.status == "completed", out.summary
    assert [a for a in _cliques(acted) if a[1].get("ref") == 36]


@pytest.mark.asyncio
async def test_clique_que_nao_envia_nao_para():
    acted: list = []
    driver, _ = _driver(
        [
            {"action": "consultar", "args": {"perguntas": [PERGUNTAS[6]]}},
            {"action": "fill", "args": {"ref": 34, "value": "Academia Exemplo"}},
            {"action": "click", "args": {"ref": 37}},
            {"action": "done", "args": {"answer": "página 1 preenchida"}},
        ],
        acted=acted, respondedor=_RespondedorFake(DA_MEMORIA),
    )
    out = await driver.run("Preencha a primeira página pela memória", start_url=URL)
    assert out.status == "completed", out.summary


@pytest.mark.asyncio
async def test_retomada_envia_o_que_foi_conferido_sem_consultar_de_novo():
    acted: list = []
    resp = _RespondedorFake([])  # se fosse chamado, responderia diferente
    driver, _ = _driver(
        [
            {"action": "consultar", "args": {"perguntas": [PERGUNTAS[1], PERGUNTAS[6]]}},
            {"action": "click", "args": {"ref": 36}},
            {"action": "done", "args": {"answer": "enviado"}},
        ],
        acted=acted, respondedor=resp,
        previas=[r.to_dict() for r in DA_MEMORIA], liberado="Enviar",
    )
    out = await driver.run("Preencha pela memória e envie", start_url=URL)
    assert out.status == "completed", out.summary
    assert resp.chamadas == []
    assert [a for a in _cliques(acted) if a[1].get("ref") == 36]
    passo = next(s for s in out.steps if s.action == "consultar")
    assert "Academia Exemplo" in passo.detail


@pytest.mark.asyncio
async def test_sem_memoria_neste_job_o_consultar_manda_deixar_em_branco():
    acted: list = []
    driver, _ = _driver(
        [
            {"action": "consultar", "args": {"perguntas": "Nome da academia?"}},
            {"action": "done", "args": {"answer": "nada a preencher"}},
        ],
        acted=acted,
    )
    out = await driver.run("Preencha pela memória", start_url=URL)
    passo = next(s for s in out.steps if s.action == "consultar")
    assert "não tenho acesso à memória" in passo.detail and "Não invente" in passo.detail


def test_consultar_sem_perguntas_e_recusado_com_o_formato_certo():
    driver, _ = _driver([], acted=[])
    with pytest.raises(ValueError, match="precisa de `perguntas`"):
        driver._validate({"action": "consultar", "args": {"perguntas": []}}, _form())
    _, args = driver._validate({"action": "consultar", "args": {"perguntas": "seu email"}}, _form())
    assert args["perguntas"] == ["seu email"]


@pytest.mark.asyncio
async def test_marcador_que_veio_da_memoria_entra_no_card_de_liberacao():
    from core.release import ReleaseFiller

    class _Servico:
        async def list_approved(self, user_id):
            return []

    class _PKS:
        async def primary_user_field(self, *, user_id, key):
            return "daniel@example.com" if key == "email" else None

        async def search_entities(self, **_):
            return []

    acted: list = []
    driver, _ = _driver(
        [
            {"action": "consultar", "args": {"perguntas": ["seu email"]}},
            {"action": "fill", "args": {"ref": 9, "value": "«pks:email»"}},
        ],
        acted=acted,
        respondedor=_RespondedorFake([Resposta("seu email", "«pks:email»", ["Daniel Morais"])]),
        filler=ReleaseFiller(_Servico(), _PKS()),
    )
    out = await driver.run("Preencha pela memória e envie", start_url=URL)
    assert out.status == "needs_release", out.summary
    assert "email" in out.release["campos"]
    assert out.respostas and out.respostas[0]["resposta"] == "«pks:email»"
    assert not [a for a in acted if a[0] == "browser.fill"]


# ── 3. o runner entrega a conferência íntegra e guarda as respostas ──────────


def test_runner_poe_a_conferencia_na_frente_do_caderno_e_no_resumo():
    from core.jobs.runner_browser_local import _com_revisao, _respostas_para_sessao

    outcome = SimpleNamespace(
        status="needs_confirmation",
        revisao=["«seu email» → «pks:email» — o seu dado, só com a sua liberação"],
        respostas=[{"pergunta": "seu email", "resposta": "«pks:email»"}],
    )
    summary, itens = _com_revisao(outcome, "Preenchi e parei.", ["nota do caderno"])
    assert itens == [outcome.revisao[0], "nota do caderno"]
    assert "O que vai no formulário:\n- «seu email»" in summary
    assert _respostas_para_sessao(outcome) == {"respostas_memoria": outcome.respostas}
    concluido = SimpleNamespace(status="completed", respostas=outcome.respostas)
    assert _respostas_para_sessao(concluido) == {}


def test_runner_monta_o_respondedor_so_com_memoria():
    from core.jobs.runner_browser_local import _build_respondedor

    job = SimpleNamespace(
        id=uuid4(), user_id=uuid4(), title="Formulário de treino",
        spec_json={"task": "t", "pedido": "preencha e envie"},
    )
    assert _build_respondedor(None, object(), "m", job) is None
    assert _build_respondedor(object(), object(), "m", job) is None  # PKS sem o método
    pks = SimpleNamespace(evidencia_de_formulario=lambda **_: None)
    r = _build_respondedor(pks, object(), "m", job)
    assert isinstance(r, Respondedor)
    assert "preencha e envie" in r._finalidade and "Formulário de treino" in r._finalidade


# ── 4. as duas retomadas herdam as respostas ───────────────────────────────


def test_continuacao_por_liberacao_herda_as_respostas():
    from core.jobs.release_resume import spec_de_continuacao

    respostas = [Resposta("seu email", "«pks:email»", ["Daniel Morais"]).to_dict()]
    parado = SimpleNamespace(
        id=uuid4(), spec_json={"task": "t", "pedido": "preencha e envie"},
        extra_metadata={"browser_session": {"tab_ref": 7, "url": URL, "respostas_memoria": respostas}},
    )
    row = SimpleNamespace(id=uuid4(), campos=["pks:email"], destino="docs.google.com")
    assert spec_de_continuacao(parado, row)["respostas_memoria"] == respostas


@pytest.mark.asyncio
async def test_retomada_do_clique_herda_as_respostas_conferidas():
    from core.jobs.executor import JobToolExecutor

    respostas = [Resposta("seu email", "«pks:email»", ["Daniel Morais"]).to_dict()]
    anterior = SimpleNamespace(
        id=uuid4(), title="Formulário de treino", status="waiting",
        spec_json={"task": "t", "pedido": "preencha e envie"},
        extra_metadata={"browser_session": {"pending": "Enviar", "tab_ref": 7, "url": URL,
                                            "respostas_memoria": respostas}},
    )
    criados: list = []

    class _DB:
        async def create_agent_job(self, job):
            job.id = uuid4()
            criados.append(job)
            return job

        async def update_agent_job(self, *_a, **_k):
            return None

    ex = JobToolExecutor.__new__(JobToolExecutor)
    ex.db = _DB()
    ex.jm = SimpleNamespace(poke=lambda: None)
    ex.user_id = uuid4()
    ex.session_id = uuid4()
    ex.device_id = "dev-1"

    async def _dono(job_id):
        return anterior

    async def _livre():
        return None

    ex._get_owned = _dono
    ex._browser_local_em_curso = _livre
    out = await ex._retomada(anterior.id)
    assert out["ok"] is True
    spec = criados[0].spec_json
    assert spec["respostas_memoria"] == respostas
    assert spec["resume_pending"] == "Enviar"


def test_a_ferramenta_manda_o_cerebro_nao_escrever_respostas_da_memoria():
    from core.jobs.tools import JOB_TOOLS

    tool = next(t for t in JOB_TOOLS if t.name == "browser_local_task")
    texto = tool.parameters["task"].description
    assert "responda cada pergunta pela memória" in texto
    assert "DITOU" in texto
