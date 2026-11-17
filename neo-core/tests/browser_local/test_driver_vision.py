"""O que o driver ganhou depois do fiasco do Google Drive (2026-08-28).

Naquele dia dois jobs queimaram os 25 passos clicando no MESMO elemento: o
`click` devolvia `ok:true` (o evento foi despachado) e o histórico lia
"25 sucessos". Faltavam três coisas, e é isto que está sob teste aqui:

1. perceber que a tela NÃO mudou depois de uma ação;
2. recusar a repetição literal dessa ação;
3. mandar um print junto do texto, para o modelo ver o que o DOM não conta.

Como no irmão ``test_driver``, o canal e o LLM são dublês: o que está sob
teste é o julgamento do driver, não a rede.
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
    """Devolve os passos roteirizados e guarda o que recebeu."""

    def __init__(
        self, steps: list[dict], *, explode: bool = False, boom: Exception | None = None
    ) -> None:
        self.steps = list(steps)
        self.calls: list[dict] = []
        self.explode = explode
        # `boom` deixa o teste escolher QUAL falha — o driver trata cota
        # estourada (definitiva) e timeout (passageiro) de formas diferentes.
        self.boom = boom or RuntimeError("timeout falando com o modelo de visão")
        # Quando setado, este provider é o OLHO: responde TEXTO sobre a imagem,
        # não JSON de ação. É a diferença de papel que o desenho novo criou.
        self.answer: str | None = None

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.explode:
            raise self.boom
        if self.answer is not None:
            return _FakeResponse(self.answer)
        step = (
            self.steps.pop(0)
            if self.steps
            else {"action": "give_up", "args": {"reason": "sem roteiro"}}
        )
        return _FakeResponse(json.dumps(step))


class _FakeLLM:
    """Um provider por modelo — é assim que se testa a queda da visão."""

    def __init__(self, by_model: dict[str, _FakeProvider], default: _FakeProvider) -> None:
        self.by_model = by_model
        self.default = default

    def get_provider_for_model(self, model: str):
        return self.by_model.get(model, self.default)


def _snapshot(marker: str = "a", **over):
    """Tela do Drive: a pasta só existe no TEXTO; o botão é anônimo."""
    base = {
        "url": "https://drive.google.com/drive/my-drive",
        "title": "Meu Drive",
        "ready": "complete",
        "stop": False,
        "elements": [
            {"ref": 0, "tag": "button", "text": "Pesquisar no Google Drive"},
            {"ref": 1, "tag": "div", "type": "row", "text": "Print"},
            {"ref": 2, "tag": "button", "text": "Mais ações", "owner": "Print"},
        ],
        "text": f"Print em Meu Drive Mais ações ({marker})",
    }
    base.update(over)
    return base


def _driver(steps, *, snapshots=None, vision_model=None, acted=None, **kwargs):
    """``snapshots``: lista consumida em ordem (a última se repete para sempre)."""
    acted = acted if acted is not None else []
    queue = list(snapshots) if snapshots else [_snapshot()]

    async def act(action, args, timeout):
        acted.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return queue.pop(0) if len(queue) > 1 else queue[0]
        if action == "browser.screenshot":
            return {"ok": True, "format": "jpeg", "data": "UFJJTlQ=", "marks": True}
        if action == "browser.downloads":
            return {
                "ok": True,
                "dir": "/Users/daniel/Downloads",
                "count": 1,
                "files": [
                    {"name": "Print-20260829.zip", "bytes": 4096, "seconds_ago": 3,
                     "incomplete": False}
                ],
            }
        if action in ("browser.click", "browser.dblclick", "browser.right_click"):
            return {"ok": True, "label": "Mais ações"}
        return {"ok": True}

    provider = _FakeProvider(steps)
    driver = BrowserLocalDriver(
        act=act,
        llm=_FakeLLM({}, provider),
        model="texto",
        vision_model=vision_model,
        **kwargs,
    )
    driver._acted = acted  # type: ignore[attr-defined]
    driver._provider = provider  # type: ignore[attr-defined]
    return driver


def _driver_com_olho(olho, texto, *, snapshots=None):
    """Driver com DOIS modelos distintos: o que decide e o que enxerga.

    É o dublê que o desenho de visão sob demanda exige — com um provider só não
    dá para provar que a imagem foi para um e a decisão veio do outro.
    """
    pedidos: list = []
    queue = list(snapshots) if snapshots else [_snapshot()]

    async def act(action, args, timeout):
        pedidos.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return queue.pop(0) if len(queue) > 1 else queue[0]
        if action == "browser.screenshot":
            return {"ok": True, "data": "UFJJTlQ=", "marks": True}
        if action in ("browser.click", "browser.dblclick", "browser.right_click"):
            return {"ok": True, "label": "Mais ações"}
        return {"ok": True}

    driver = BrowserLocalDriver(
        act=act,
        llm=_FakeLLM({"visao": olho, "texto": texto}, texto),
        model="texto",
        vision_model="visao",
    )
    driver._pedidos = pedidos  # type: ignore[attr-defined]
    return driver


# ── 1. a tela não mudou ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clique_que_nao_muda_a_tela_e_carimbado_no_historico():
    driver = _driver(
        [
            {"action": "click", "args": {"ref": 2}},
            {"action": "done", "args": {"answer": "pronto"}},
        ]
    )
    result = await driver.run("baixa a pasta Print")
    clique = next(s for s in result.steps if s.action == "click")
    assert "a tela NÃO mudou" in clique.detail


@pytest.mark.asyncio
async def test_tela_que_muda_nao_carimba_nada():
    driver = _driver(
        [
            {"action": "click", "args": {"ref": 2}},
            {"action": "done", "args": {"answer": "pronto"}},
        ],
        snapshots=[_snapshot("a"), _snapshot("b"), _snapshot("b")],
    )
    result = await driver.run("baixa a pasta Print")
    clique = next(s for s in result.steps if s.action == "click")
    assert "NÃO mudou" not in (clique.detail or "")


@pytest.mark.asyncio
async def test_extract_no_meio_nao_conta_como_passo_inutil():
    """`extract` não muda a tela por definição — cobrar dela seria falso positivo."""
    driver = _driver(
        [
            {"action": "extract", "args": {}},
            {"action": "extract", "args": {}},
            {"action": "done", "args": {"answer": "pronto"}},
        ]
    )
    result = await driver.run("lê a página")
    assert all("NÃO mudou" not in (s.detail or "") for s in result.steps)


# ── 2. repetir o que não funcionou ──────────────────────────────────────


@pytest.mark.asyncio
async def test_repeticao_do_clique_inutil_e_recusada():
    """O loop de 2026-08-28: 25 cliques iguais. Agora o segundo nem sai."""
    driver = _driver(
        [
            {"action": "click", "args": {"ref": 2}},
            {"action": "click", "args": {"ref": 2}},  # o mesmo de novo
            {"action": "right_click", "args": {"ref": 1}},
            {"action": "done", "args": {"answer": "pronto"}},
        ]
    )
    result = await driver.run("baixa a pasta Print")
    cliques = [a for a, _ in driver._acted if a == "browser.click"]
    assert len(cliques) == 1, "a repetição não pode chegar à página"
    assert any("escolha outra" in (s.detail or "") for s in result.steps)
    # E o caminho alternativo passa normalmente.
    assert ("browser.right_click", {"ref": 1, "tab_ref": 42}) in driver._acted


@pytest.mark.asyncio
async def test_ref_em_texto_nao_burla_a_guarda():
    """{"ref": "2"} é o mesmo passo que {"ref": 2} — a guarda normaliza antes."""
    driver = _driver(
        [
            {"action": "click", "args": {"ref": 2}},
            {"action": "click", "args": {"ref": "2"}},
            {"action": "done", "args": {"answer": "pronto"}},
        ]
    )
    await driver.run("baixa a pasta Print")
    assert len([a for a, _ in driver._acted if a == "browser.click"]) == 1


@pytest.mark.asyncio
async def test_repetir_e_permitido_quando_a_tela_mudou():
    driver = _driver(
        [
            {"action": "click", "args": {"ref": 2}},
            {"action": "click", "args": {"ref": 2}},
            {"action": "done", "args": {"answer": "pronto"}},
        ],
        snapshots=[_snapshot("a"), _snapshot("b"), _snapshot("c")],
    )
    await driver.run("baixa a pasta Print")
    assert len([a for a, _ in driver._acted if a == "browser.click"]) == 2


# ── 3. o print no prompt ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_decidir_nunca_recebe_imagem_a_visao_e_sob_demanda():
    """O passo do meio do desenho: quem VÊ não é quem DECIDE.

    Até 2026-09-02 a imagem ia em todo passo e, com ela, o modelo de visão
    virava o raciocinador — e um modelo de visão barato é ruim nisso: nos jobs
    9593f55f/db7adabf/f79d815d ele decidiu 36 de 37 passos, teve 33% de recusa e
    emitiu `done` SEM o `answer` nas 10 vezes que tentou terminar. Agora a
    decisão é sempre do modelo de texto, sem imagem, e a visão só entra quando
    pedida."""
    olho = _FakeProvider([])
    texto = _FakeProvider([{"action": "done", "args": {"answer": "pronto"}}])
    driver = _driver_com_olho(olho, texto)

    await driver.run("baixa a pasta Print")

    assert not olho.calls, "sem `look`, o modelo de visão nem é chamado"
    assert all(c["model"] == "texto" for c in texto.calls)
    assert all(c["messages"][-1].images is None for c in texto.calls)


@pytest.mark.asyncio
async def test_look_manda_a_imagem_para_o_olho_e_a_resposta_vira_historico():
    """`look` é a ponte: print → modelo de visão → texto no histórico.

    A resposta dos olhos precisa VOLTAR para quem dirige, senão a imagem foi
    paga à toa. Ela entra como detalhe do passo, que é o que o prompt do passo
    seguinte já carrega."""
    olho = _FakeProvider([])
    olho.answer = "SIM, o campo 7 está preenchido com 'teclado'"  # type: ignore[attr-defined]
    texto = _FakeProvider(
        [
            {"action": "look", "args": {"pergunta": "o campo 7 ficou preenchido?"}},
            {"action": "done", "args": {"answer": "pronto"}},
        ]
    )
    driver = _driver_com_olho(olho, texto)

    result = await driver.run("preenche a busca")

    pedido = next(a for a in driver._pedidos if a[0] == "browser.screenshot")[1]
    assert pedido["marks"] is True, "sem os números, a resposta não vira ação"

    chamada = olho.calls[0]
    assert chamada["model"] == "visao"
    assert chamada["messages"][-1].images == ["UFJJTlQ="]
    assert "o campo 7 ficou preenchido?" in chamada["messages"][-1].content
    # Os olhos NÃO recebem o vocabulário de ações — o papel deles é responder.
    assert "action" not in chamada["messages"][0].content

    passo = next(s for s in result.steps if s.action == "look")
    assert "SIM" in passo.detail


@pytest.mark.asyncio
async def test_olhar_duas_vezes_a_mesma_tela_e_recusado():
    """A imagem é o insumo caro: repetir na mesma tela devolveria a mesma
    resposta. A recusa aponta a saída em vez de só barrar."""
    olho = _FakeProvider([])
    texto = _FakeProvider(
        [
            {"action": "look", "args": {"pergunta": "o que tem aqui?"}},
            {"action": "look", "args": {"pergunta": "e agora?"}},
            {"action": "done", "args": {"answer": "pronto"}},
        ]
    )
    driver = _driver_com_olho(olho, texto)

    result = await driver.run("olha a tela")

    assert len(olho.calls) == 1, "a segunda olhada na mesma tela nem sai"
    recusa = next(s for s in result.steps if s.args.get("recusado"))
    assert recusa.action == "look" and "já olhou" in recusa.detail


@pytest.mark.asyncio
async def test_tela_travada_faz_o_driver_olhar_sozinho():
    """A rede do desenho. O modelo comanda o `look`, mas cegueira repetida é o
    ponto cego conhecido do DOM: em 2026-08-28 o driver clicou 25 vezes no
    mesmo botão porque `ok:true` não quer dizer que a página reagiu."""
    olho = _FakeProvider([])
    olho.answer = "Tem um banner de cookies cobrindo a lista; o botão 9 fecha."  # type: ignore[attr-defined]
    texto = _FakeProvider(
        [
            {"action": "click", "args": {"ref": 2}},
            {"action": "click", "args": {"ref": 1}},
            {"action": "done", "args": {"answer": "pronto"}},
        ]
    )
    driver = _driver_com_olho(olho, texto)

    result = await driver.run("baixa a pasta Print")

    assert len(olho.calls) == 1, "uma imagem por episódio de travamento, não por passo"
    automatico = next(s for s in result.steps if s.action == "look" and s.args.get("automático"))
    assert "banner" in automatico.detail


@pytest.mark.asyncio
async def test_olhar_nao_gasta_o_orcamento_de_passos():
    """Conferir não pode encurtar a tarefa.

    Se `look` consumisse passo, o desenho estaria cobrando justamente o
    comportamento que quer incentivar — e o caminho mais barato para o modelo
    seria agir no escuro. O que limita a olhada é a trava de tela repetida, não
    o orçamento."""
    olho = _FakeProvider([])
    olho.answer = "a lista tem 5 itens"  # type: ignore[attr-defined]
    texto = _FakeProvider(
        [
            {"action": "look", "args": {"pergunta": "quantos itens?"}},
            {"action": "click", "args": {"ref": 2}},
            {"action": "look", "args": {"pergunta": "mudou?"}},
            {"action": "done", "args": {"answer": "pronto"}},
        ]
    )
    # Duas telas distintas para as duas olhadas serem legítimas.
    driver = _driver_com_olho(
        olho, texto, snapshots=[_snapshot("a"), _snapshot("b"), _snapshot("b")]
    )
    driver.max_steps = 2  # só `click` e `done` cabem no orçamento

    result = await driver.run("baixa a pasta Print")

    assert result.status == "completed", "as duas olhadas não podem ter comido o orçamento"
    assert len([s for s in result.steps if s.action == "look"]) == 2


@pytest.mark.asyncio
async def test_sem_visao_o_modelo_fica_cego_mas_o_usuario_nao():
    """Cegueira é do MODELO, e parava de ser do usuário junto — não é mais.

    Durante a tarefa o driver não gasta print nenhum: a imagem não tem para
    quem ir, e o prompt avisa "SEM VISÃO" em vez de deixá-lo queimar um passo
    pedindo `look` para ouvir "não deu". No FIM ele fotografa uma vez, porque
    quem lê aquele print é uma pessoa — e "dois jobs inteiros rodaram cegos sem
    ninguém saber" é exatamente o que a visão opcional custou uma vez.
    """
    driver = _driver([{"action": "done", "args": {"answer": "pronto"}}])
    result = await driver.run("baixa a pasta Print")

    fotos = [args for a, args in driver._acted if a == "browser.screenshot"]
    assert len(fotos) == 1, "um print, no fim — nenhum durante a tarefa"
    assert fotos[0].get("marks") is False, "o print de quem lê não leva os números"
    assert result.screenshots, "job cego ainda mostra onde parou"
    assert result.screenshot_captions[0] == "a tela quando terminei"

    call = driver._provider.calls[0]
    assert call["model"] == "texto"
    assert call["messages"][-1].images is None
    # E o prompt AVISA que não há olhos, em vez de deixá-lo gastar um passo
    # pedindo `look` para ouvir "não deu".
    assert "SEM VISÃO" in call["messages"][-1].content


@pytest.mark.asyncio
async def test_print_que_falha_desliga_a_visao_e_a_tarefa_segue():
    acted: list = []

    async def act(action, args, timeout):
        acted.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return _snapshot()
        if action == "browser.screenshot":
            raise RuntimeError("a aba não deixa fotografar")
        return {"ok": True}

    provider = _FakeProvider(
        [
            {"action": "look", "args": {"pergunta": "o que tem na tela?"}},
            {"action": "look", "args": {"pergunta": "e agora?"}},
            {"action": "done", "args": {"answer": "pronto"}},
        ]
    )
    driver = BrowserLocalDriver(
        act=act, llm=_FakeLLM({}, provider), model="texto", vision_model="visao"
    )
    result = await driver.run("baixa a pasta Print")

    assert result.status == "completed"
    assert driver.vision_model is None, "uma falha basta — não paga o timeout todo passo"
    # Tentou uma vez só; o segundo `look` nem chega a pedir print.
    assert len([a for a, _ in acted if a == "browser.screenshot"]) == 1
    # E o modelo é AVISADO, em texto, de que não adianta pedir de novo.
    olhada = next(s for s in result.steps if s.action == "look")
    assert "não consegui ver" in olhada.detail
    assert all(call["model"] == "texto" for call in provider.calls)


@pytest.mark.asyncio
async def test_soluco_da_visao_nao_cega_a_tarefa_inteira():
    """UMA falha passageira do modelo de visão custa só AQUELE passo.

    Antes bastava um erro qualquer para desligar a visão pelo job inteiro. No
    job 0f87265f foi um 429 no passo 1 — e os 20 passos seguintes correram no
    escuro, clicando "Baixar" 10 vezes e deixando 5 cópias de um APK de 52 MB
    na pasta do usuário. Erro passageiro agora vale um passo, não a tarefa."""
    olho = _FakeProvider([], explode=True)
    texto = _FakeProvider(
        [
            {"action": "look", "args": {"pergunta": "o que tem na tela?"}},
            {"action": "done", "args": {"answer": "pronto"}},
        ]
    )
    driver = _driver_com_olho(olho, texto)

    result = await driver.run("baixa a pasta Print")

    assert result.status == "completed"
    assert driver.vision_model == "visao", "um soluço não pode cegar o resto do job"
    assert driver._vision_strikes == 1
    # A tarefa segue: o `look` devolve o motivo em texto e quem dirige decide
    # com o DOM, que é o que ele já tinha.
    olhada = next(s for s in result.steps if s.action == "look")
    assert "não consegui ver" in olhada.detail
    assert all(c["messages"][-1].images is None for c in texto.calls)


@pytest.mark.asyncio
async def test_cota_estourada_desliga_a_visao_de_vez_e_diz_por_que():
    """Cota/crédito não volta sozinho: insistir só paga latência por passo. Mas
    o usuário PRECISA saber que o trabalho saiu às cegas — foi a ausência desse
    aviso que fez o job 0f87265f reportar sucesso limpo com a pasta cheia."""
    olho = _FakeProvider(
        [],
        explode=True,
        boom=RuntimeError(
            "Error code: 429 - {'message': 'You have no credits remaining.', "
            "'code': 'credit_balance_exhausted'}"
        ),
    )
    texto = _FakeProvider(
        [
            {"action": "look", "args": {"pergunta": "o que tem na tela?"}},
            {"action": "done", "args": {"answer": "pronto"}},
        ]
    )
    driver = _driver_com_olho(olho, texto)

    result = await driver.run("baixa a pasta Print")

    assert result.status == "completed"
    assert driver.vision_model is None, "cota estourada não se resolve tentando de novo"
    assert driver._vision_down_reason and "crédito" in driver._vision_down_reason
    # E o fecho conta a cegueira, em vez de entregar um "pronto" limpo.
    assert "SEM ver a tela" in result.summary


@pytest.mark.asyncio
async def test_visao_que_volta_a_responder_zera_os_soluços():
    """Três timeouts espalhados por uma tarefa longa não podem somar e desligar
    uma visão que está funcionando entre eles."""
    olho = _FakeProvider([], explode=True)
    texto = _FakeProvider(
        [
            {"action": "look", "args": {"pergunta": "o que tem na tela?"}},
            {"action": "done", "args": {"answer": "pronto"}},
        ]
    )
    driver = _driver_com_olho(olho, texto)
    await driver.run("baixa a pasta Print")
    assert driver._vision_strikes == 1

    driver._vision_strikes = 2
    olho.explode = False
    olho.answer = "a tela mostra a lista de arquivos"
    assert "lista" in await driver._look("o que tem na tela?", _snapshot())
    assert driver._vision_strikes == 0


# ── vocabulário novo e o dono da linha ──────────────────────────────────


@pytest.mark.asyncio
async def test_dblclick_e_right_click_chegam_ao_device():
    driver = _driver(
        [
            {"action": "dblclick", "args": {"ref": 1}},
            {"action": "right_click", "args": {"ref": 1}},
            {"action": "done", "args": {"answer": "pronto"}},
        ],
        snapshots=[_snapshot("a"), _snapshot("b"), _snapshot("c"), _snapshot("c")],
    )
    result = await driver.run("abre a pasta Print")
    acoes = [a for a, _ in driver._acted]
    assert "browser.dblclick" in acoes and "browser.right_click" in acoes
    assert result.status == "completed"


def test_o_dono_da_linha_aparece_no_texto_da_pagina():
    """É o que separa o 'Mais ações' da pasta Print do dos outros 69."""
    rendered = BrowserLocalDriver._render(_snapshot())
    assert '[2] button "Mais ações" em "Print"' in rendered
    assert '[1] div(row) "Print"' in rendered


# ── a CAIXA que não é botão (job a0eab88c) ──────────────────────────────
#
# Na busca do Drive os 4 resultados vinham dentro de UM [role=row]. O modelo deu
# click/dblclick/right_click nessa caixa 23 vezes: o evento era despachado com
# sucesso num contêiner sem handler, o driver respondia "ok" e nada acontecia. A
# guarda de repetição não pegava porque ele alternava a AÇÃO no mesmo ref.


def _com_caixa():
    """Tela de busca do Drive, como o coletor a devolve hoje.

    Os números batem com o que o JS produz para essa árvore: a `row` embrulha os
    dois `gridcell` (grupo=2), cada `gridcell` embrulha o seu kebab, e `dentro`
    aponta sempre para o ref do pai. Repare que o rótulo da caixa é a
    CONCATENAÇÃO dos filhos — é por isso que casar por substring não serve.
    """
    return _snapshot(
        elements=[
            {"ref": 0, "tag": "button", "text": "Pesquisar no Google Drive",
             "box": [12, 8, 220, 40], "chrome": True},
            {"ref": 1, "tag": "div", "type": "row", "grupo": 2,
             "text": "palworldMais açõesPalworld-SteamRIP.com.rarMais ações",
             "box": [240, 180, 1174, 188]},
            {"ref": 2, "tag": "div", "type": "gridcell", "text": "palworld Pasta",
             "box": [240, 180, 900, 44], "dentro": 1},
            {"ref": 3, "tag": "button", "text": "Mais ações", "owner": "palworld Pasta",
             "box": [1100, 190, 24, 24], "dentro": 2},
            {"ref": 4, "tag": "div", "type": "gridcell",
             "text": "Palworld-SteamRIP.com.rar Desconhecido",
             "box": [240, 224, 900, 44], "dentro": 1},
            {"ref": 5, "tag": "button", "text": "Mais ações",
             "owner": "Palworld-SteamRIP.com.rar Desconhecido",
             "box": [1100, 234, 24, 24], "dentro": 4},
        ]
    )


def test_a_caixa_se_anuncia_como_caixa_e_o_tamanho_aparece():
    """Com a visão desligada, o tamanho é a única pista de forma que sobra."""
    rendered = BrowserLocalDriver._render(_com_caixa())
    assert "CAIXA com 2 itens dentro" in rendered
    # E aponta PARA ONDE ir — sem os números a recusa não resolve nada.
    assert "→ use [2], [4]" in rendered
    assert "1174×188px" in rendered, "a caixa tem que declarar que é enorme"
    assert "24×24px" in rendered, "e o kebab, que é minúsculo"
    assert "[moldura do site]" in rendered


@pytest.mark.asyncio
async def test_clicar_na_caixa_e_recusado_antes_de_tocar_na_pagina():
    driver = _driver(
        [
            {"action": "dblclick", "args": {"ref": 1}},
            {"action": "dblclick", "args": {"ref": 2}},
            {"action": "done", "args": {"answer": "achei"}},
        ],
        snapshots=[_com_caixa()],
    )
    result = await driver.run("abre a pasta palworld")
    alvos = [args.get("ref") for a, args in driver._acted if a == "browser.dblclick"]
    assert alvos == [2], "só o item de verdade pode chegar à página"
    recusa = next(s.detail for s in result.steps if not s.ok)
    assert "CAIXA" in recusa
    assert '[2] "palworld Pasta"' in recusa, "a recusa tem que apontar o alvo certo"
    assert "Mais ações" not in recusa, "o kebab é neto, não item da caixa"


@pytest.mark.asyncio
async def test_a_recusa_da_caixa_nao_depende_da_acao_escolhida():
    """click, dblclick e right_click na caixa: nenhum toca na página."""
    driver = _driver(
        [
            {"action": "click", "args": {"ref": 1}},
            {"action": "dblclick", "args": {"ref": 1}},
            {"action": "right_click", "args": {"ref": 1}},
            {"action": "done", "args": {"answer": "desisti"}},
        ],
        snapshots=[_com_caixa()],
    )
    result = await driver.run("abre a pasta palworld")
    tocou = [a for a, _ in driver._acted if a.startswith("browser.") and "click" in a]
    assert tocou == [], "a alternância de ação era o furo da guarda antiga"
    assert len([s for s in result.steps if not s.ok]) == 3


# ── o painel que rola (job 9025d9f8) ────────────────────────────────────
#
# "qual o tamanho desse arquivo?": o painel de detalhes do Drive estava ABERTO
# com a resposta logo abaixo da dobra. O painel não era interativo, então não
# tinha ref nenhum — e o modelo passou 21 dos 25 passos mandando `scroll` no
# botão da aba ao lado dele.


def _com_painel():
    return _snapshot(
        text="Meu Drive palworld Palworld-SteamRIP.com.rar",
        elements=[
            {"ref": 0, "tag": "a", "text": "Drive", "box": [0, 0, 102, 48], "chrome": True},
            {"ref": 1, "tag": "button", "type": "tab", "text": "Detalhes",
             "box": [1600, 90, 154, 47]},
            {"ref": 2, "tag": "div", "text": "Painel de detalhes",
             "box": [1600, 140, 380, 860], "grupo": 3,
             "rola": [0, 1400, 860],
             "texto": "Detalhes do arquivo\nTipo\nDesconhecido\nTamanho\n1,7 GB\n"
                      "Armazenamento usado\n1,7 GB"},
        ],
    )


def test_o_texto_do_painel_entra_no_snapshot():
    """innerText não para na borda visível: a resposta vem sem rolar nada."""
    rendered = BrowserLocalDriver._render(_com_painel())
    assert "DENTRO DOS PAINÉIS QUE ROLAM (1)" in rendered
    assert "1,7 GB" in rendered, "o tamanho está abaixo da dobra e tem que aparecer"
    assert "ABAIXO da dobra" in rendered


def test_o_painel_declara_quanto_ainda_rola():
    """Sem isto o modelo rola no escuro, sem saber se ainda falta conteúdo."""
    rendered = BrowserLocalDriver._render(_com_painel())
    assert "⟪ROLA: 0 de 540⟫" in rendered  # 1400 de altura − 860 de janela
    # E o botão da aba ao lado NÃO se anuncia como rolável.
    assert '[1] button(tab) "Detalhes" 154×47px\n' in rendered + "\n"


def test_a_pagina_inteira_continua_no_texto():
    """Recortar o texto para 'a área da tarefa' jogou fora a resposta uma vez."""
    rendered = BrowserLocalDriver._render(_com_painel())
    assert "Texto da página:" in rendered
    assert "Meu Drive palworld" in rendered


@pytest.mark.asyncio
async def test_acao_que_falha_conta_como_passo_inutil():
    """O furo que deixou o MESMO scroll falhar 21 vezes sem uma recusa.

    `_pending_signature` só era atribuído no caminho de sucesso, então a falha
    não incrementava `_stuck` e a guarda anti-repetição nunca via nada.
    """
    acted: list[tuple[str, dict]] = []

    async def act(action, args, timeout):
        acted.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return _com_painel()
        if action == "browser.screenshot":
            return {"ok": True, "format": "jpeg", "data": "eA==", "marks": True}
        if action == "browser.scroll":
            raise RuntimeError("o painel recusou a rolagem programática")
        return {"ok": True}

    provider = _FakeProvider(
        [
            {"action": "scroll", "args": {"delta": 300, "ref": 1}},
            {"action": "scroll", "args": {"delta": 300, "ref": 1}},
            {"action": "done", "args": {"answer": "1,7 GB"}},
        ]
    )
    driver = BrowserLocalDriver(act=act, llm=_FakeLLM({}, provider), model="texto")
    result = await driver.run("qual o tamanho do arquivo")

    scrolls = [a for a, _ in acted if a == "browser.scroll"]
    assert len(scrolls) == 1, f"a segunda tentativa idêntica tinha que ser recusada ({scrolls})"
    assert any("escolha outra" in (s.detail or "") for s in result.steps)


# ── downloads: a barra do navegador não está na página ──────────────────


@pytest.mark.asyncio
async def test_downloads_traz_o_arquivo_que_chegou_no_disco():
    """Era o buraco que fez o Neo clicar em "Baixar" três vezes (job 0baba7c7)."""
    driver = _driver(
        [
            {"action": "downloads", "args": {}},
            {"action": "done", "args": {"answer": "baixei"}},
        ]
    )
    result = await driver.run("baixa a pasta Print")
    passo = next(s for s in result.steps if s.action == "downloads")
    assert passo.ok is True
    assert "Print-20260829.zip" in passo.detail
    assert "AINDA BAIXANDO" not in passo.detail


@pytest.mark.asyncio
async def test_arquivo_a_meio_download_e_declarado_incompleto():
    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return _snapshot()
        if action == "browser.downloads":
            return {
                "ok": True,
                "files": [
                    {"name": "Print.zip.crdownload", "bytes": 12, "seconds_ago": 1,
                     "incomplete": True}
                ],
            }
        return {"ok": True}

    provider = _FakeProvider(
        [
            {"action": "downloads", "args": {}},
            {"action": "done", "args": {"answer": "baixei"}},
        ]
    )
    driver = BrowserLocalDriver(act=act, llm=_FakeLLM({}, provider), model="texto")
    result = await driver.run("baixa a pasta Print")
    passo = next(s for s in result.steps if s.action == "downloads")
    assert "AINDA BAIXANDO" in passo.detail


@pytest.mark.asyncio
async def test_pasta_de_downloads_vazia_e_dita_com_todas_as_letras():
    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return _snapshot()
        if action == "browser.downloads":
            return {"ok": True, "files": []}
        return {"ok": True}

    provider = _FakeProvider(
        [
            {"action": "downloads", "args": {}},
            {"action": "done", "args": {"answer": "nada"}},
        ]
    )
    driver = BrowserLocalDriver(act=act, llm=_FakeLLM({}, provider), model="texto")
    result = await driver.run("baixa a pasta Print")
    passo = next(s for s in result.steps if s.action == "downloads")
    assert "nada novo" in passo.detail


def test_o_prompt_proibe_esperar_download_na_pagina():
    """A regra tem que estar escrita: o modelo não adivinha que a barra some."""
    from core.browser_local.driver import _SYSTEM

    assert "downloads" in _SYSTEM
    assert "Download completo" in _SYSTEM


# ── menu aberto: é ali que a próxima ação mora ───────────────────────────


def _com_menu(**over):
    """Drive com o menu de "Mais ações" aberto — o estado do job 706ff8a8."""
    base = _snapshot(**over)
    base["elements"] = [
        {"ref": 0, "tag": "button", "text": "Pesquisar no Google Drive"},
        *[
            {"ref": i, "tag": "button", "text": "Mais ações", "owner": f"arquivo-{i}.pdf"}
            for i in range(1, 40)
        ],
        {"ref": 88, "tag": "div", "type": "menuitem", "text": "Baixar", "menu": True},
        {"ref": 89, "tag": "div", "type": "menuitem", "text": "Renomear", "menu": True},
    ]
    return base


def test_menu_aberto_sobe_para_o_topo_da_pagina():
    rendered = BrowserLocalDriver._render(_com_menu())
    cabecalho = rendered.find("MENU/DIÁLOGO ABERTO AGORA")
    lista = rendered.find("\nElementos (")
    assert cabecalho != -1, "o menu aberto tem que ser anunciado"
    assert cabecalho < lista, "e antes da lista de 41 elementos, não depois"
    assert '[88] div(menuitem) "Baixar"' in rendered
    # E o item continua na lista completa, com o mesmo ref.
    assert rendered.count('[88] div(menuitem) "Baixar"') == 2


def test_pagina_sem_menu_nao_ganha_a_secao():
    assert "MENU/DIÁLOGO ABERTO" not in BrowserLocalDriver._render(_snapshot())


@pytest.mark.asyncio
async def test_recusa_por_repeticao_lista_os_itens_do_menu_aberto():
    """A recusa muda de 'não' para 'clique aqui' — era o que faltava."""
    driver = _driver(
        [
            {"action": "click", "args": {"ref": 3}},
            {"action": "click", "args": {"ref": 3}},  # reabrir o menu de novo
            {"action": "click", "args": {"ref": 88}},
            {"action": "done", "args": {"answer": "baixei"}},
        ],
        snapshots=[_com_menu()],
    )
    result = await driver.run("baixa a pasta Print")
    recusa = next(s for s in result.steps if s.args.get("recusado"))
    assert "menu já está ABERTO" in recusa.detail
    assert '[88] "Baixar"' in recusa.detail
    # E o clique certo, sugerido pela própria recusa, passa.
    assert any(a == "browser.click" and args.get("ref") == 88 for a, args in driver._acted)


@pytest.mark.asyncio
async def test_travar_em_quatro_recusas_conta_o_motivo_real():
    driver = _driver([{"action": "hackear", "args": {}}] * 5)
    result = await driver.run("faz algo")
    assert result.status == "failed"
    assert "Travei" in result.summary
    assert "não existe" in result.summary, "o resumo tem que dizer QUAL foi a recusa"


# ── rolagem: "ok" sem sair do lugar é mentira ────────────────────────────


@pytest.mark.asyncio
async def test_scroll_que_nao_rolou_vira_passo_FALHO_com_o_motivo():
    """No Drive o `scrollBy` da janela não move a lista — e isso matou um job."""

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return _snapshot()
        if action == "browser.scroll":
            raise RuntimeError("o painel não rolou (já está no fim?) — tente a busca do site")
        return {"ok": True}

    provider = _FakeProvider(
        [
            {"action": "scroll", "args": {"delta": 600}},
            {"action": "done", "args": {"answer": "parei"}},
        ]
    )
    driver = BrowserLocalDriver(act=act, llm=_FakeLLM({}, provider), model="texto")
    result = await driver.run("acha a pasta Print")
    passo = next(s for s in result.steps if s.action == "scroll")
    assert passo.ok is False
    assert "não rolou" in passo.detail
    # E o usuário é avisado de que houve tropeço no caminho.
    assert "falhou" in result.summary or "falharam" in result.summary


@pytest.mark.asyncio
async def test_scroll_pode_mirar_um_painel_pelo_ref():
    driver = _driver(
        [
            {"action": "scroll", "args": {"delta": 600, "ref": 1}},
            {"action": "done", "args": {"answer": "pronto"}},
        ],
        snapshots=[_snapshot("a"), _snapshot("b"), _snapshot("b")],
    )
    await driver.run("rola a lista")
    assert ("browser.scroll", {"delta": 600, "ref": 1, "tab_ref": 42}) in driver._acted


@pytest.mark.asyncio
async def test_teclas_de_lista_longa_estao_no_vocabulario():
    driver = _driver(
        [
            {"action": "press", "args": {"key": "PageDown"}},
            {"action": "done", "args": {"answer": "pronto"}},
        ],
        snapshots=[_snapshot("a"), _snapshot("b"), _snapshot("b")],
    )
    result = await driver.run("desce a lista")
    assert result.status == "completed"
    assert ("browser.press", {"key": "PageDown", "tab_ref": 42}) in driver._acted


# ── o painel de progresso não é um menu de escolha ──────────────────────


def _com_painel_download(**over):
    """O que o Drive mostra depois do "Baixar" (job 2c8cc496, passo 5)."""
    base = _snapshot(**over)
    base["elements"] = [
        {"ref": 46, "tag": "button", "text": "Mais ações", "owner": "Print"},
        {"ref": 88, "tag": "div", "type": "button", "text": "Minimizar", "menu": True},
        {"ref": 89, "tag": "div", "type": "button", "text": "Fechar", "menu": True},
        {"ref": 90, "tag": "div", "type": "button", "text": "Cancelar o download", "menu": True},
    ]
    return base


@pytest.mark.asyncio
async def test_nao_deixa_cancelar_o_download_que_ele_mesmo_comecou():
    """O erro do job 2c8cc496: clicou "Baixar" e em seguida "Cancelar o download"."""
    acted: list = []

    async def act(action, args, timeout):
        acted.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return _com_painel_download()
        if action == "browser.click":
            # O rótulo vem do DEVICE — é ele que diz que um download começou.
            return {"ok": True, "label": "Baixar" if args.get("ref") == 46 else "Cancelar o download"}
        return {"ok": True}

    provider = _FakeProvider(
        [
            {"action": "click", "args": {"ref": 46}},   # dispara o download
            {"action": "click", "args": {"ref": 90}},   # "Cancelar o download"
            {"action": "done", "args": {"answer": "fim"}},
        ]
    )
    driver = BrowserLocalDriver(act=act, llm=_FakeLLM({}, provider), model="texto")
    result = await driver.run("baixa a pasta Print")

    cliques = [args.get("ref") for a, args in acted if a == "browser.click"]
    assert 90 not in cliques, "cancelar o próprio download não pode chegar à página"
    recusa = next(s for s in result.steps if s.args.get("recusado"))
    assert "CANCELA o download" in recusa.detail
    assert "wait_s" in recusa.detail, "a recusa tem que dizer o que fazer no lugar"


@pytest.mark.asyncio
async def test_sem_download_nosso_o_botao_de_cancelar_nao_e_barrado():
    """A guarda é sobre destruir NOSSO trabalho — não é censura ao verbo."""
    acted: list = []

    async def act(action, args, timeout):
        acted.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return _com_painel_download()
        if action == "browser.click":
            return {"ok": True, "label": "Cancelar o download"}
        return {"ok": True}

    provider = _FakeProvider(
        [
            {"action": "click", "args": {"ref": 90}},
            {"action": "done", "args": {"answer": "cancelei"}},
        ]
    )
    driver = BrowserLocalDriver(act=act, llm=_FakeLLM({}, provider), model="texto")
    await driver.run("cancela aquele download que ficou pendurado")
    assert 90 in [args.get("ref") for a, args in acted if a == "browser.click"]


def test_o_cabecalho_do_menu_avisa_sobre_painel_de_progresso():
    rendered = BrowserLocalDriver._render(_com_painel_download())
    assert "painel de PROGRESSO" in rendered
    assert "não feche e não cancele" in rendered


@pytest.mark.asyncio
async def test_downloads_com_wait_s_espera_o_arquivo_ficar_pronto():
    """Baixar uma PASTA compacta primeiro: a primeira olhada vem vazia."""
    respostas = [
        {"ok": True, "files": []},                                                   # compactando
        {"ok": True, "files": [{"name": "Print.zip.crdownload", "bytes": 10,
                                "seconds_ago": 1, "incomplete": True}]},             # vindo
        {"ok": True, "files": [{"name": "Print.zip", "bytes": 4096,
                                "seconds_ago": 1, "incomplete": False}]},            # chegou
    ]
    olhadas = []

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return _snapshot()
        if action == "browser.downloads":
            olhadas.append(1)
            return respostas.pop(0) if respostas else {"ok": True, "files": []}
        return {"ok": True}

    provider = _FakeProvider(
        [
            {"action": "downloads", "args": {"wait_s": 30}},
            {"action": "done", "args": {"answer": "baixei"}},
        ]
    )
    driver = BrowserLocalDriver(act=act, llm=_FakeLLM({}, provider), model="texto")
    result = await driver.run("baixa a pasta Print")

    passo = next(s for s in result.steps if s.action == "downloads")
    assert "Print.zip" in passo.detail and "AINDA BAIXANDO" not in passo.detail
    assert len(olhadas) >= 3, "tem que insistir até o arquivo ficar pronto"


@pytest.mark.asyncio
async def test_downloads_sem_wait_s_olha_uma_vez_so():
    olhadas = []

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return _snapshot()
        if action == "browser.downloads":
            olhadas.append(1)
            return {"ok": True, "files": []}
        return {"ok": True}

    provider = _FakeProvider(
        [
            {"action": "downloads", "args": {}},
            {"action": "done", "args": {"answer": "nada"}},
        ]
    )
    driver = BrowserLocalDriver(act=act, llm=_FakeLLM({}, provider), model="texto")
    result = await driver.run("olha os downloads")
    # baseline no começo da tarefa + a do passo + o delta no fim = 3, e só.
    assert len(olhadas) == 3, "sem wait_s o passo não pode ficar insistindo"
    assert "nada novo" in next(s for s in result.steps if s.action == "downloads").detail


# ── compactar É o download (job 0611add0: 10 passos brigando com o painel) ──


def _com_painel_progresso(**over):
    base = _snapshot(**over)
    base["elements"] = [
        {"ref": 46, "tag": "button", "text": "Mais ações", "owner": "Print"},
        {"ref": 88, "tag": "div", "type": "button", "text": "Minimizar",
         "menu": True, "panel": "Preparando o download"},
        {"ref": 89, "tag": "div", "type": "button", "text": "Fechar",
         "menu": True, "panel": "Preparando o download"},
        {"ref": 90, "tag": "div", "type": "button", "text": "Cancelar o download",
         "menu": True, "panel": "Preparando o download"},
    ]
    return base


def _com_dialogo_de_cancelamento(**over):
    base = _snapshot(**over)
    base["elements"] = [
        {"ref": 92, "tag": "button", "text": "Continuar download",
         "menu": True, "panel": "Cancelar o download?"},
        {"ref": 93, "tag": "button", "text": "Cancelar download",
         "menu": True, "panel": "Cancelar o download?"},
    ]
    return base


async def _com_download_iniciado(snapshot, roteiro):
    """Driver que já disparou um download (o device carimbou o rótulo "Baixar")."""
    acted: list = []

    async def act(action, args, timeout):
        acted.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return snapshot
        if action == "browser.click":
            return {"ok": True, "label": "Baixar" if args.get("ref") == 46 else "Fechar"}
        return {"ok": True}

    driver = BrowserLocalDriver(
        act=act,
        llm=_FakeLLM({}, _FakeProvider([{"action": "click", "args": {"ref": 46}}] + roteiro)),
        model="texto",
    )
    result = await driver.run("baixa a pasta Print")
    return acted, result


@pytest.mark.asyncio
async def test_fechar_o_painel_de_progresso_e_recusado():
    """O X do painel do Drive abre "Cancelar o download?" — não é dispensar popup."""
    acted, result = await _com_download_iniciado(
        _com_painel_progresso(),
        [{"action": "click", "args": {"ref": 89}}, {"action": "done", "args": {"answer": "fim"}}],
    )
    assert 89 not in [args.get("ref") for a, args in acted if a == "browser.click"]
    recusa = next(s for s in result.steps if s.args.get("recusado"))
    assert "Compactar/preparar é a primeira fase" in recusa.detail
    assert "wait_s" in recusa.detail


@pytest.mark.asyncio
async def test_minimizar_o_painel_de_progresso_tambem_e_recusado():
    acted, _ = await _com_download_iniciado(
        _com_painel_progresso(),
        [{"action": "click", "args": {"ref": 88}}, {"action": "done", "args": {"answer": "fim"}}],
    )
    assert 88 not in [args.get("ref") for a, args in acted if a == "browser.click"]


@pytest.mark.asyncio
async def test_continuar_download_no_dialogo_continua_permitido():
    """A guarda é sobre o PAINEL de progresso — o diálogo de confirmação tem
    uma resposta certa, e ela precisa passar."""
    acted, result = await _com_download_iniciado(
        _com_dialogo_de_cancelamento(),
        [{"action": "click", "args": {"ref": 92}}, {"action": "done", "args": {"answer": "fim"}}],
    )
    assert 92 in [args.get("ref") for a, args in acted if a == "browser.click"]
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_sem_download_nosso_fechar_o_painel_e_livre():
    acted: list = []

    async def act(action, args, timeout):
        acted.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return _com_painel_progresso()
        return {"ok": True, "label": "Fechar"}

    driver = BrowserLocalDriver(
        act=act,
        llm=_FakeLLM({}, _FakeProvider([
            {"action": "click", "args": {"ref": 89}},
            {"action": "done", "args": {"answer": "fim"}},
        ])),
        model="texto",
    )
    await driver.run("fecha aquele painel aí")
    assert 89 in [args.get("ref") for a, args in acted if a == "browser.click"]


def test_o_render_diz_de_qual_painel_o_item_veio():
    """"Fechar" num menu de escolha e "Fechar" no painel de download são coisas
    diferentes — e antes disso o modelo não tinha como saber qual era qual."""
    rendered = BrowserLocalDriver._render(_com_painel_progresso())
    assert '[89] div(button) "Fechar" [painel: "Preparando o download"]' in rendered


def test_o_prompt_explica_que_compactar_e_parte_do_download():
    from core.browser_local.driver import _SYSTEM

    assert "compactar/preparar É o download" in _SYSTEM
    assert "Compactando" in _SYSTEM, "o nome que o Drive usa tem que estar escrito"
    # O texto é quebrado em linhas — casar só o começo da frase.
    assert "não arrume a tela" in _SYSTEM


# ── stale: o elemento sumiu entre a foto e o clique ──────────────────────


@pytest.mark.asyncio
async def test_elemento_que_ficou_stale_e_reencontrado_pelo_rotulo():
    """A SPA redesenha nos ~1,5s que o modelo leva para decidir. O passo não
    pode morrer por isso — o rótulo continua sendo o mesmo."""
    telas = [
        _snapshot("a"),
        # mesma tela, mas o botão foi recriado com outro ref
        {**_snapshot("a"), "elements": [
            {"ref": 7, "tag": "button", "text": "Mais ações", "owner": "Print"},
        ]},
    ]
    acted: list = []
    passou_stale = {"ok": False}
    vistas = {"n": 0}

    async def act(action, args, timeout):
        acted.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            vistas["n"] += 1
            return telas[0] if vistas["n"] == 1 else telas[1]
        if action == "browser.click":
            if args.get("ref") == 2 and not passou_stale["ok"]:
                passou_stale["ok"] = True
                raise RuntimeError("stale")
            return {"ok": True, "label": "Mais ações"}
        return {"ok": True}

    driver = BrowserLocalDriver(
        act=act,
        llm=_FakeLLM({}, _FakeProvider([
            {"action": "click", "args": {"ref": 2}},
            {"action": "done", "args": {"answer": "fim"}},
        ])),
        model="texto",
    )
    result = await driver.run("abre o menu da pasta Print")

    cliques = [args.get("ref") for a, args in acted if a == "browser.click"]
    assert cliques == [2, 7], "tinha que refazer no ref novo do MESMO rótulo"
    passo = next(s for s in result.steps if s.action == "click")
    assert passo.ok is True, "o passo não pode morrer por um ref que envelheceu"


@pytest.mark.asyncio
async def test_stale_sem_o_rotulo_na_tela_continua_falhando():
    """Se o elemento sumiu de verdade, inventar outro seria pior que falhar."""

    vistas = {"n": 0}

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            vistas["n"] += 1
            # A primeira tela tem o botão; na hora de refazer, ele sumiu de vez.
            elementos = (
                [{"ref": 0, "tag": "button", "text": "Some daqui a pouco"}]
                if vistas["n"] == 1
                else [{"ref": 0, "tag": "button", "text": "Outra coisa"}]
            )
            return {**_snapshot(), "elements": elementos}
        if action == "browser.click":
            raise RuntimeError("stale")
        return {"ok": True}

    driver = BrowserLocalDriver(
        act=act,
        llm=_FakeLLM({}, _FakeProvider([
            {"action": "click", "args": {"ref": 0}},
            {"action": "done", "args": {"answer": "fim"}},
        ])),
        model="texto",
    )
    result = await driver.run("clica em algo")
    passo = next(s for s in result.steps if s.action == "click")
    assert passo.ok is False
    assert "não está mais na tela" in passo.detail


# ── "chegou" tem que ser dito, não inferido (job 6142ed04) ───────────────


async def _driver_com_downloads(respostas, roteiro, task="baixa a pasta Print"):
    acted: list = []
    fila = list(respostas)

    async def act(action, args, timeout):
        acted.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return {**_snapshot(), "elements": [
                {"ref": 0, "tag": "button", "text": "Mais ações", "owner": "Print"},
                {"ref": 1, "tag": "div", "type": "menuitem", "text": "Baixar", "menu": True},
            ]}
        if action == "browser.downloads":
            return fila.pop(0) if len(fila) > 1 else fila[0]
        if action == "browser.click":
            return {"ok": True, "label": "Baixar"}
        return {"ok": True}

    driver = BrowserLocalDriver(
        act=act, llm=_FakeLLM({}, _FakeProvider(roteiro)), model="texto"
    )
    return acted, await driver.run(task)


_VAZIO = {"ok": True, "files": []}
_PRONTO = {"ok": True, "files": [
    {"name": "Print-2026.zip", "bytes": 888218, "seconds_ago": 0, "incomplete": False}
]}


@pytest.mark.asyncio
async def test_arquivo_completo_e_anunciado_com_todas_as_letras():
    """Antes o retorno era só "nome (888218 bytes, 0s atrás)" e o modelo tinha
    que INFERIR o fim pela ausência de "AINDA BAIXANDO". Não inferiu."""
    _, result = await _driver_com_downloads(
        [_VAZIO, _PRONTO, _PRONTO],
        [
            {"action": "downloads", "args": {"wait_s": 30}},
            {"action": "done", "args": {"answer": "baixei"}},
        ],
    )
    passo = next(s for s in result.steps if s.action == "downloads")
    assert "DOWNLOAD CONCLUÍDO" in passo.detail
    assert "Print-2026.zip" in passo.detail
    assert "NÃO mande baixar de novo" in passo.detail
    assert "responda `done`" in passo.detail


@pytest.mark.asyncio
async def test_baixar_de_novo_o_que_ja_chegou_e_barrado_uma_vez():
    acted, result = await _driver_com_downloads(
        [_VAZIO, _PRONTO, _PRONTO],
        [
            {"action": "downloads", "args": {"wait_s": 30}},
            {"action": "click", "args": {"ref": 1}},          # "Baixar" de novo
            {"action": "done", "args": {"answer": "baixei"}},
        ],
    )
    assert not [a for a, _ in acted if a == "browser.click"], "não pode rebaixar"
    recusa = next(s for s in result.steps if s.args.get("recusado"))
    assert "JÁ está no disco" in recusa.detail
    assert "responda `done` agora" in recusa.detail


@pytest.mark.asyncio
async def test_insistindo_no_baixar_a_guarda_sai_da_frente():
    """Tarefa de VÁRIOS arquivos existe — o aviso é único, não uma cerca."""
    acted, _ = await _driver_com_downloads(
        [_VAZIO, _PRONTO, _PRONTO],
        [
            {"action": "downloads", "args": {"wait_s": 30}},
            {"action": "click", "args": {"ref": 1}},   # barrado
            {"action": "click", "args": {"ref": 1}},   # insistiu → passa
            {"action": "done", "args": {"answer": "baixei os dois"}},
        ],
    )
    assert [args.get("ref") for a, args in acted if a == "browser.click"] == [1]


@pytest.mark.asyncio
async def test_arquivo_a_meio_caminho_nao_e_anunciado_como_concluido():
    vindo = {"ok": True, "files": [
        {"name": "Print.zip.crdownload", "bytes": 12, "seconds_ago": 1, "incomplete": True}
    ]}
    _, result = await _driver_com_downloads(
        [vindo, vindo, vindo],
        [
            {"action": "downloads", "args": {}},
            {"action": "done", "args": {"answer": "esperando"}},
        ],
    )
    passo = next(s for s in result.steps if s.action == "downloads")
    assert "CONCLUÍDO" not in passo.detail
    assert "AINDA BAIXANDO" in passo.detail


# ── "baixe" cumprido quando começa; quem espera é o vigia ────────────────

_VINDO = {"ok": True, "files": [
    {"name": "Print.zip.crdownload", "bytes": 4096, "seconds_ago": 1, "incomplete": True}
]}


@pytest.mark.asyncio
async def test_download_que_comecou_ja_cumpre_um_pedido_de_baixar():
    _, result = await _driver_com_downloads(
        [_VAZIO, _VINDO, _VINDO],
        [
            {"action": "downloads", "args": {"wait_s": 60}},
            {"action": "done", "args": {"answer": "começou"}},
        ],
        task="baixa a pasta Print do meu drive",
    )
    passo = next(s for s in result.steps if s.action == "downloads")
    assert "DOWNLOAD COMEÇOU" in passo.detail
    assert "a tarefa está cumprida" in passo.detail
    assert "NÃO fique esperando" in passo.detail


@pytest.mark.asyncio
async def test_pedido_que_manda_aguardar_continua_esperando_a_conclusao():
    """"baixe e aguarde a conclusão" é outro pedido — aí sim vale gastar passo."""
    _, result = await _driver_com_downloads(
        [_VAZIO, _VINDO, _PRONTO],
        [
            {"action": "downloads", "args": {"wait_s": 30}},
            {"action": "done", "args": {"answer": "baixei"}},
        ],
        task="baixe a pasta Print e aguarde a conclusão do download",
    )
    passo = next(s for s in result.steps if s.action == "downloads")
    assert "DOWNLOAD CONCLUÍDO" in passo.detail, "com pedido de espera, espera até o fim"


@pytest.mark.parametrize(
    "pedido,acompanha",
    [
        ("baixa a pasta Print", False),
        ("abre o drive e baixa o zip da pasta Print", False),
        ("baixe o arquivo e aguarde a conclusão dele", True),
        ("baixe o arquivo e garanta que tudo ocorra bem", True),
        ("baixa a pasta e espera terminar", True),
        ("baixe o zip e confirme que o download foi concluído", True),
    ],
)
def test_so_pedido_explicito_liga_o_acompanhamento(pedido, acompanha):
    from core.browser_local.driver import _ASKS_TO_FOLLOW

    assert bool(_ASKS_TO_FOLLOW.search(pedido)) is acompanha


@pytest.mark.parametrize(
    "pedido,acompanha",
    [
        ("baixe o arquivo e aguarde a conclusão dele", True),
        ("baixa a pasta e espera terminar", True),
        ("exporte o relatório e aguarde o download terminar", True),
        ("baixa a pasta Print", False),
        # As mesmas frases em tarefa sem download. Medido nos 164 pedidos
        # `browser_local` do banco em 05/11: 16 dos 32 que ligavam eram assim
        # (questionário, rastreio de pedido, formulário), e os 16 que continuam
        # ligando são todos downloads do Drive.
        ("Avance até o fim e chegue na tela de resultado", False),
        ("role a página para baixo até o final e leia os comentários", False),
        ("aguarde a página carregar e clique em Entrar", False),
        ("garanta que todas as perguntas sejam respondidas", False),
    ],
)
def test_frase_de_espera_so_liga_o_acompanhamento_quando_o_pedido_fala_de_download(
    pedido, acompanha
):
    from core.browser_local.driver import _pede_acompanhar_download

    assert _pede_acompanhar_download(pedido) is acompanha


@pytest.mark.asyncio
async def test_o_contador_do_prompt_e_do_agente_e_nao_da_pagina():
    """Job 7b73f755: o modelo leu "Passo 8 de no máximo 53" como o tamanho do
    quiz, concluiu que estava no modo longo e reiniciou em loop. A página estava
    em "Question 1 of 36"."""

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return _snapshot()
        return {"ok": True}

    provider = _FakeProvider([{"action": "done", "args": {"answer": "ok"}}])
    driver = BrowserLocalDriver(act=act, llm=_FakeLLM({}, provider), model="texto")
    await driver.run("responda o questionário de 36 perguntas")
    prompt = provider.calls[0]["messages"][1].content
    assert "Passo 1 de no máximo" not in prompt
    assert "contador SEU, não da página" in prompt.strip().splitlines()[-1]


@pytest.mark.asyncio
async def test_o_arquivo_a_meio_caminho_fica_registrado_para_o_vigia():
    acted, _ = await _driver_com_downloads(
        [_VAZIO, _VINDO, _VINDO],
        [
            {"action": "downloads", "args": {}},
            {"action": "done", "args": {"answer": "começou"}},
        ],
        task="baixa a pasta Print",
    )
    # o runner usa este campo para enfileirar o job download_watch
    assert acted, "o canal foi exercitado"


@pytest.mark.asyncio
async def test_driver_expoe_o_download_pendente():
    vistas = {"n": 0}

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return _snapshot()
        if action == "browser.downloads":
            vistas["n"] += 1
            return _VAZIO if vistas["n"] == 1 else _VINDO  # a 1ª é a linha de base
        return {"ok": True}

    driver = BrowserLocalDriver(
        act=act,
        llm=_FakeLLM({}, _FakeProvider([
            {"action": "downloads", "args": {}},
            {"action": "done", "args": {"answer": "começou"}},
        ])),
        model="texto",
    )
    await driver.run("baixa a pasta Print")
    assert driver.download_pending == "Print.zip.crdownload"
    assert driver.follow_download is False


# ── a trava que faltava: conferir antes de baixar de novo ───────────────────
def _drive_com_download(acted, arquivos=None):
    """Drive onde cada passo muda a tela (como no job real: menus abrindo e
    fechando). Sem isso a guarda de "ação repetida" barra antes e mascara a
    trava que está sendo testada."""
    passo = {"n": 0}

    async def act(action, args, timeout):
        acted.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            passo["n"] += 1
            # Dois botões de baixar, como no Drive real (menu de contexto e
            # barra de ações): é o TEXTO do snapshot que a guarda lê antes de
            # deixar o clique sair.
            return _snapshot(
                marker=str(passo["n"]),
                elements=[
                    {"ref": 0, "tag": "button", "text": "Pesquisar no Google Drive"},
                    {"ref": 1, "tag": "button", "text": "Baixar", "owner": "app-release.apk"},
                    {"ref": 2, "tag": "button", "text": "Fazer o download mesmo assim"},
                ],
            )
        if action == "browser.click":
            return {"ok": True, "label": "Baixar"}
        if action == "browser.downloads":
            return {"ok": True, "files": list(arquivos or [])}
        return {"ok": True}

    return act


@pytest.mark.asyncio
async def test_nao_dispara_dois_downloads_sem_conferir_no_meio():
    """Reprodução do job 0f87265f: cego, o agente clicou "Baixar" e, sem nunca
    olhar a pasta, clicou de novo — 10 cliques de download, 2 conferências (uma
    cedo demais), 5 cópias de um APK de 52 MB no disco do usuário.

    A trava antiga (`_download_done`) só armava DEPOIS de uma conferência
    bem-sucedida, ou seja, dependia exatamente do que não estava acontecendo.
    Esta arma no PRIMEIRO clique e não depende de enxergar a tela."""
    acted: list[tuple[str, dict]] = []
    texto = _FakeProvider(
        [
            {"action": "click", "args": {"ref": 2}},   # dispara o download
            {"action": "click", "args": {"ref": 1}},   # cego: manda baixar de novo
            {"action": "done", "args": {"answer": "fim"}},
        ]
    )
    driver = BrowserLocalDriver(
        act=_drive_com_download(acted), llm=_FakeLLM({}, texto), model="texto"
    )
    result = await driver.run("baixa o app-release.apk")

    cliques = [a for a, _ in acted if a == "browser.click"]
    assert len(cliques) == 1, f"o segundo download não podia chegar à página: {cliques}"
    recusa = next(s for s in result.steps if s.args.get("recusado"))
    assert "JÁ disparou um download" in recusa.detail
    assert "downloads" in recusa.detail, "a recusa tem que dizer COMO destravar"


@pytest.mark.asyncio
async def test_conferir_a_pasta_destrava_o_proximo_download():
    """A trava não pode prender: quando a conferência diz que nada chegou (o
    aviso de vírus do Drive barrou, por exemplo), clicar de novo é legítimo —
    e foi exatamente esse o passo 6 do job real."""
    acted: list[tuple[str, dict]] = []
    texto = _FakeProvider(
        [
            {"action": "click", "args": {"ref": 2}},
            {"action": "downloads", "args": {"wait_s": 1}},   # confere: nada chegou
            {"action": "click", "args": {"ref": 1}},          # agora pode
            {"action": "done", "args": {"answer": "fim"}},
        ]
    )
    driver = BrowserLocalDriver(
        act=_drive_com_download(acted), llm=_FakeLLM({}, texto), model="texto"
    )
    result = await driver.run("baixa o app-release.apk")

    cliques = [a for a, _ in acted if a == "browser.click"]
    assert len(cliques) == 2, "conferir tem que liberar o clique seguinte"
    assert not [s for s in result.steps if s.args.get("recusado")]


@pytest.mark.asyncio
async def test_cancelar_download_nao_e_confundido_com_baixar_de_novo():
    """"Cancelar o download" contém a palavra "download" e faz o OPOSTO. A
    trava nova não pode acusá-lo de duplicar arquivo — quem barra esse botão é
    a guarda de cancelamento, com a mensagem dela."""
    from core.browser_local.driver import _starts_download

    assert _starts_download("Baixar") is True
    assert _starts_download("Fazer o download mesmo assim") is True
    assert _starts_download("Cancelar o download") is False
    assert _starts_download("Cancel download") is False
    assert _starts_download("Mais ações") is False


@pytest.mark.asyncio
async def test_reler_o_mesmo_elemento_na_mesma_tela_e_recusado():
    """No job f27970d6 o mesmo `extract {"ref": 51}` rodou 7 vezes seguidas
    devolvendo a mesma palavra. `extract` não muda a tela por definição, então
    a guarda de atuação não o alcançava — e nada mais o cobria."""
    driver = _driver(
        [
            {"action": "extract", "args": {"ref": 2}},
            {"action": "extract", "args": {"ref": 2}},
            {"action": "extract", "args": {"ref": 1}},  # outro elemento: passa
            {"action": "done", "args": {"answer": "pronto"}},
        ]
    )
    result = await driver.run("qual o tamanho do arquivo")
    lidos = [args.get("ref") for a, args in driver._acted if a == "browser.extract"]
    assert lidos == [2, 1], f"a releitura idêntica tinha que ser recusada ({lidos})"
    assert any("já leu esse elemento" in (s.detail or "") for s in result.steps)


@pytest.mark.asyncio
async def test_tela_nova_libera_a_releitura():
    """A trava é por TELA, não para sempre: depois que a página muda, reler o
    mesmo elemento é legítimo — o texto dele pode ter mudado junto."""
    driver = _driver(
        [
            {"action": "extract", "args": {"ref": 2}},
            {"action": "click", "args": {"ref": 2}},
            {"action": "extract", "args": {"ref": 2}},
            {"action": "done", "args": {"answer": "pronto"}},
        ],
        snapshots=[_snapshot("a"), _snapshot("a"), _snapshot("b"), _snapshot("b")],
    )
    await driver.run("lê de novo depois de mudar a tela")
    lidos = [args.get("ref") for a, args in driver._acted if a == "browser.extract"]
    assert lidos == [2, 2]


# ── 5. o caderno ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_o_caderno_vira_a_entrega():
    """Anotar é o que transforma "vi" em "tenho"."""
    driver = _driver(
        [
            {"action": "note", "args": {"texto": "Redragon K552 — R$ 189,90 — 4,7★"}},
            {"action": "note", "args": {"texto": "HyperX Alloy — R$ 229,00 — 4,5★"}},
            {"action": "done", "args": {"answer": "achei dois dentro da faixa"}},
        ]
    )
    result = await driver.run("me traga teclados até R$250 com preço e nota")

    assert result.status == "completed"
    assert result.notes == [
        "Redragon K552 — R$ 189,90 — 4,7★",
        "HyperX Alloy — R$ 229,00 — 4,5★",
    ]
    assert "Redragon K552" in result.summary and "HyperX Alloy" in result.summary


@pytest.mark.asyncio
async def test_tarefa_interrompida_ENTREGA_o_que_ja_tinha():
    """O ponto do desenho. Fim ruim não pode apagar o que já foi coletado.

    No job fc47220e o usuário apertou PARE numa tarefa que tinha visto 73 preços
    dentro da faixa pedida, e recebeu de volta só "Parei porque você apertou
    PARE" — nada do que passou na tela sobrou. Um caderno com 2 itens vale mais
    que uma tarefa "limpa" com zero.
    """
    telas = [_snapshot("a"), _snapshot("b"), dict(_snapshot("c"), stop=True)]
    driver = _driver(
        [
            {"action": "note", "args": {"texto": "Redragon K552 — R$ 189,90"}},
            {"action": "click", "args": {"ref": 2}},
            {"action": "click", "args": {"ref": 1}},
        ],
        snapshots=telas,
    )
    result = await driver.run("me traga teclados até R$250")

    assert result.status == "stopped"
    assert "Redragon K552" in result.summary, "o que foi achado tem que voltar mesmo assim"
    assert result.notes == ["Redragon K552 — R$ 189,90"]


@pytest.mark.asyncio
async def test_anotar_nao_gasta_o_orcamento_nem_pede_nada_ao_device():
    """Coletar não pode encurtar a tarefa, e anotar não toca na página."""
    driver = _driver(
        [
            {"action": "note", "args": {"texto": "item 1"}},
            {"action": "note", "args": {"texto": "item 2"}},
            {"action": "click", "args": {"ref": 2}},
            {"action": "done", "args": {"answer": "fim"}},
        ],
        snapshots=[_snapshot("a"), _snapshot("b"), _snapshot("b")],
    )
    driver.max_steps = 2  # só `click` e `done` cabem

    result = await driver.run("coleta duas coisas")

    assert result.status == "completed", "as anotações não podem comer o orçamento"
    assert len(result.notes) == 2
    # Nenhuma ida ao navegador por causa de `note`.
    assert not [a for a, _ in driver._acted if a == "browser.note"]


@pytest.mark.asyncio
async def test_o_caderno_viaja_no_prompt_de_todo_passo():
    """Sem isso o modelo não sabe o que já tem — e recomeça ou duplica."""
    driver = _driver(
        [
            {"action": "note", "args": {"texto": "Redragon K552 — R$ 189,90"}},
            {"action": "done", "args": {"answer": "fim"}},
        ]
    )
    await driver.run("coleta teclados")

    ultimo = driver._provider.calls[-1]["messages"][-1].content
    assert "SEU CADERNO (1)" in ultimo
    assert "Redragon K552" in ultimo


@pytest.mark.asyncio
async def test_caderno_vazio_por_muitos_passos_gera_cutucada():
    """Regra no prompt não bastou: o job fc47220e andou 23 passos, viu 73 preços
    dentro da faixa e não anotou nada. A cutucada é uma linha, uma vez."""
    passos = [{"action": "click", "args": {"ref": 2}} for _ in range(7)]
    passos.append({"action": "done", "args": {"answer": "fim"}})
    # Telas sempre diferentes: o clique não pode ser barrado por repetição.
    telas = [_snapshot(chr(97 + i)) for i in range(12)]
    driver = _driver(passos, snapshots=telas)

    await driver.run("me traga os 5 melhores teclados")

    prompts = [c["messages"][-1].content for c in driver._provider.calls]
    assert not any("caderno continua VAZIO" in p for p in prompts[:4]), "cedo demais"
    assert any("caderno continua VAZIO" in p for p in prompts), "nunca cutucou"


@pytest.mark.asyncio
async def test_o_link_de_cada_item_vai_no_snapshot():
    """"Colete o link de cada item" é metade das tarefas de coleta na web.

    O device já mandava `href` e o render jogava fora — então o modelo ou
    inventava o endereço (`/dp/B07...`, medido em 2026-09-03) ou gastava um
    passo entrando em cada item só para ler a barra de endereços. Link do mesmo
    site aparece como caminho; de fora, inteiro.
    """
    tela = _snapshot(
        elements=[
            {"ref": 1, "tag": "a", "text": "Redragon K552",
             "href": "https://www.amazon.com.br/dp/B08K1?ref=sr_1&pd_rd=abc"},
            {"ref": 2, "tag": "a", "text": "Review no TechTudo",
             "href": "https://www.techtudo.com.br/teclados"},
            {"ref": 3, "tag": "button", "text": "Filtrar"},
        ],
        url="https://www.amazon.com.br/s?k=teclado",
    )
    render = BrowserLocalDriver._render(tela)

    assert "→ /dp/B08K1?ref=sr_1&pd_rd=abc" in render, "mesmo site vira caminho"
    assert "→ https://www.techtudo.com.br/teclados" in render, "site de fora vai inteiro"
    # Botão não tem href e não ganha seta solta.
    assert '[3] button "Filtrar"' in render and "[3] button \"Filtrar\" →" not in render


@pytest.mark.asyncio
async def test_link_gigante_e_cortado():
    """São até 200 elementos por snapshot: link inteiro dobraria o prompt de
    qualquer página de listagem."""
    tela = _snapshot(
        elements=[{"ref": 1, "tag": "a", "text": "item",
                   "href": "https://x.com/" + "a" * 400}],
        url="https://outro.com/",
    )
    linha = next(l for l in BrowserLocalDriver._render(tela).splitlines() if "[1]" in l)
    assert len(linha) < 200


@pytest.mark.asyncio
async def test_caderno_nao_repete_o_que_a_resposta_ja_contou():
    """Quando termina bem, o modelo escreve a resposta A PARTIR do caderno —
    colar tudo de novo entregaria a mesma lista duas vezes ao usuário."""
    driver = _driver(
        [
            {"action": "note", "args": {"texto": "Redragon K552 — R$ 189,90"}},
            {"action": "note", "args": {"texto": "HyperX Alloy — R$ 229,00"}},
            {"action": "done", "args": {"answer": "Achei: Redragon K552 — R$ 189,90 e mais um."}},
        ]
    )
    result = await driver.run("coleta teclados")

    assert result.summary.count("Redragon K552") == 1, "o que a resposta já disse não se repete"
    assert "HyperX Alloy" in result.summary, "o que ela esqueceu ainda é entregue"
