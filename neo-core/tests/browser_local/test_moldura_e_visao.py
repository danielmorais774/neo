"""O job que girou lendo o menu da Amazon (`29c739d4`).

Pedido: "veja se tem alguma compra para chegar na Amazon". Resposta certa:
nenhuma — os 5 pedidos dos últimos 3 meses estavam todos entregues. O job gastou
20 ações em 75,6s, terminou `failed`, e a mensagem que chegou ao usuário trazia,
como "o que consegui coletar", o menu de atalhos de teclado da Amazon.

O motor tinha cinco camadas, e cada uma sozinha teria salvado o job:

  1. **A eleição do conteúdo principal** escolheu o wrapper da página inteira
     (rótulo "Ir para", os skip-links) em 10 dos 15 snapshots. O critério era
     "o maior texto ganha", e como `closest(CHROME)` só rejeita quem ESTÁ dentro
     da moldura — nunca um `<div>` que a EMBRULHA — ele converge sempre para a
     raiz. Provado no device, em `snapshot-lab/conteudo-principal.py`.
  2. **As duas janelas de texto se sobrepunham**: `main[0:2200]` e
     `corpo[0:1500]` da MESMA string. A deduplicação existia para isso e não
     tinha como funcionar, porque o corpo chegava pré-cortado em 1500 — o que
     sobrava era subconjunto do principal, vinha vazio, e o piso de 200
     caracteres restaurava a moldura DUPLICADA.
  3. **A recusa de releitura mentia**: afirmava, sempre, que "o conteúdo dos
     painéis já vem inteiro no snapshot". Não havia painel nenhum e o texto
     estava cortado. Cada vez que o modelo concluía, corretamente, que o dado
     não estava no texto, a recusa respondia com autoridade que estava.
  4. **O aviso de corte mandava ROLAR**, e o texto sai em ordem de documento:
     rolar não muda o prefixo. O modelo obedeceu sete vezes.
  5. **O caderno não registrava a visão.** Dois `look` responderam exatamente o
     que a tarefa pedia; nenhum virou nota, e o resumo final saiu com a única
     nota que existia — a moldura lida por engano.

Aqui ficam as camadas 2 a 5, que são do core. A 1 é do device.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.browser_local.driver import _VISAO_SEM_RESPOSTA, BrowserLocalDriver

from .test_driver import _driver, _snapshot

# A moldura real da página de pedidos, encurtada: é ela que ocupava o orçamento.
MOLDURA = "Ir para\nConteúdo principal\nAtalhos do teclado\n" + "\n".join(
    f"Departamento {i}" for i in range(40)
)
PEDIDOS = (
    "Pedido realizado 21 de setembro de 2026\nPedido nº 000-0000000-0000000\n"
    "Entregue no dia 3 outubro\nProduto Exemplo 113 g"
)


# ── camada 2: as duas janelas não se sobrepõem ──────────────────────────────


def test_corpo_nao_repete_a_moldura_que_o_principal_ja_trouxe():
    """O orçamento do corpo não paga duas vezes pelas mesmas linhas."""
    render = BrowserLocalDriver._render(
        _snapshot(main={"label": "Pedidos", "text": PEDIDOS}, text=PEDIDOS + "\n" + MOLDURA)
    )
    assert "Entregue no dia 3 outubro" in render
    assert render.count("Entregue no dia 3 outubro") == 1
    # A moldura, que só existe no corpo, continua lá uma vez.
    assert render.count("Departamento 7") == 1


def test_corpo_ganha_teto_curto_quando_existe_principal():
    """Com conteúdo eleito, o corpo é fallback — não merece o orçamento inteiro.

    O que o job 29c739d4 pagava: 2500 caracteres de menu no prompt, ao lado de
    2200 caracteres do MESMO menu eleitos como "conteúdo principal".
    """
    corpo = "\n".join(f"linha de moldura {i:04d}" for i in range(400))
    com_main = BrowserLocalDriver._render(
        _snapshot(main={"label": "Pedidos", "text": PEDIDOS}, text=corpo)
    )
    sem_main = BrowserLocalDriver._render(_snapshot(main=None, text=corpo))
    bloco = lambda r: r.split("Texto da página:")[1]  # noqa: E731
    assert len(bloco(com_main)) < len(bloco(sem_main))


# ── camada 4: o aviso de corte não manda mais rolar ─────────────────────────


def test_aviso_de_corte_nao_manda_rolar():
    """`body.innerText` sai em ordem de documento: rolar não traz o resto."""
    render = BrowserLocalDriver._render(_snapshot(main=None, text="x" * 9000))
    assert "ROLAR NÃO TRAZ O RESTO" in render
    assert "role a página para ver o resto" not in render


def test_corte_do_bloco_principal_e_avisado():
    """Quando o principal continua, a saída é `extract` no ref — não rolagem."""
    render = BrowserLocalDriver._render(
        _snapshot(main={"label": "Pedidos", "text": PEDIDOS, "cut": True}, text="")
    )
    assert "este bloco CONTINUA" in render
    assert "extract" in render


# ── camada 3: a recusa só afirma o que o snapshot sustenta ──────────────────


def test_recusa_nao_promete_snapshot_inteiro_quando_houve_corte():
    """A frase que realimentou o giro, cinco vezes."""
    saida = BrowserLocalDriver._onde_esta_o_texto(
        _snapshot(main={"label": "Pedidos", "text": PEDIDOS, "cut": True}, text_cut=True)
    )
    assert "CORTADO" in saida
    assert "já vem inteiro" not in saida
    assert "rolando" in saida  # diz explicitamente que rolar não resolve


def test_recusa_aponta_o_painel_quando_existe_painel():
    """Com painel na tela, a promessa é verdadeira e continua valendo."""
    elements = [{"ref": 3, "text": "Detalhes", "texto": "conteúdo do painel"}]
    saida = BrowserLocalDriver._onde_esta_o_texto(_snapshot(elements=elements))
    assert "já vem inteiro no snapshot" in saida


def test_recusa_manda_agir_quando_o_texto_esta_completo():
    """Sem painel e sem corte: "não está nesta tela" é a verdade útil."""
    saida = BrowserLocalDriver._onde_esta_o_texto(_snapshot(text="curto", text_cut=False))
    assert "INTEIRO no snapshot, sem corte" in saida
    assert "done" in saida


def test_recusa_nao_afirma_nada_com_device_antigo():
    """A janela entre o deploy do core e o restart do `tauri dev`.

    O device antigo corta o texto (1500/2200, literais) e não reporta. Sem a
    chave `text_cut`, "não houve corte" é DESCONHECIDO — e afirmar completude
    aqui recriaria a mesma mentira que esta função existe para desfazer, só com
    outra roupa.
    """
    antigo = _snapshot(text="curto")
    assert "text_cut" not in antigo
    saida = BrowserLocalDriver._onde_esta_o_texto(antigo)
    assert "Não sei dizer" in saida
    assert "INTEIRO" not in saida


# ── camada 5: a visão vai para o caderno ────────────────────────────────────


@pytest.mark.asyncio
async def test_resposta_da_visao_entra_no_caderno():
    """A resposta que o usuário deveria ter recebido, e não recebeu."""
    driver = _driver([])
    resposta = (
        "NÃO. Todos os pedidos visíveis estão com status Entregue no dia [data]. "
        "Não há nenhum pedido A caminho ou Ainda não enviado nesta tela."
    )
    driver._anotar_visao(resposta)
    assert any("Entregue" in n for n in driver.notes)
    # E chega ao usuário mesmo com o desfecho ruim.
    assert "Entregue" in driver._com_caderno("Parei: 12 passos sem acrescentar nada.")


@pytest.mark.asyncio
async def test_visao_curta_tambem_entra():
    """O piso de 200 do `extract` descartaria a resposta real (~150 chars)."""
    driver = _driver([])
    driver._anotar_visao("Todos os 5 pedidos aparecem como Entregue. Nenhum a caminho.")
    assert driver.notes


@pytest.mark.asyncio
async def test_nao_vi_nao_e_observacao():
    """Anotar o motivo da falha no lugar do conteúdo é pior que não anotar."""
    driver = _driver([])
    driver._anotar_visao(f"{_VISAO_SEM_RESPOSTA} — visão desligada nesta tarefa")
    assert not driver.notes


@pytest.mark.asyncio
async def test_visao_repetida_nao_rende_duas_notas():
    """O caderno vai para o usuário: duas cópias da mesma frase é ruído."""
    driver = _driver([])
    for _ in range(4):
        driver._anotar_visao("A lista mostra 5 pedidos, todos com status Entregue no dia.")
    assert len(driver.notes) == 1


@pytest.mark.asyncio
async def test_nota_de_visao_nao_devolve_passo_ao_orcamento():
    """Olhar não muda a página — e o freio de giro mede exatamente isso.

    `_contabilizar_producao` devolve um passo ao orçamento a cada nota nova. Se
    `look` rendesse passo, o freio que existe para detectar "nada está mudando"
    viraria acelerador: foi o que o replay do job 6633351e pegou, transformando
    um `failed` por giro em `completed`.
    """
    driver = _driver([])
    teto = driver._teto_de_passos()
    driver._anotar_visao("A lista mostra 5 pedidos, todos com status Entregue no dia.")
    driver._contabilizar_producao()
    assert driver._teto_de_passos() == teto


# ── camada 5b: leitura de moldura não vira nota ─────────────────────────────


@pytest.mark.asyncio
async def test_leitura_de_moldura_nao_entra_no_caderno():
    """A primeira cópia do menu passava — e era a única nota do job."""
    driver = _driver([])

    async def act(action, args, timeout):
        return {"ok": True, "label": "Ir para", "moldura": True, "text": MOLDURA}

    driver._act = act
    detalhe = await driver._perform("extract", {}, _snapshot())
    assert not driver.notes
    assert "MOLDURA do site" in detalhe


# ── as duas verdades não podem divergir ─────────────────────────────────────


def test_mapa_da_extensao_esta_em_dia_com_o_rust():
    """O inspetor da extensão é GERADO do `js_snapshot`. Divergir é mentir.

    `neo-map.generated.js` é o MESMO código que o device injeta por CDP, extraído
    para o inspetor visual desenhar na tela o que o agente enxerga. Um inspetor
    que vê diferente do driver não testa nada — e esta base já mentiu com
    autoridade uma vez justamente assim.

    O `--check` existia desde sempre e não tinha onde rodar: não há CI neste
    repositório. Fica aqui, na suíte que é de fato executada, porque mexer na
    eleição do conteúdo principal sem regerar o mapa é exatamente o erro que
    passa desapercebido.
    """
    script = (
        Path(__file__).resolve().parents[3] / "neo-browser-local/scripts/gerar-mapa.py"
    )
    if not script.exists():
        pytest.skip("neo-browser-local não está ao lado deste repositório")
    r = subprocess.run(
        [sys.executable, str(script), "--check"], capture_output=True, text=True
    )
    assert r.returncode == 0, (
        "neo-map.generated.js está desatualizado — rode "
        f"`python3 {script.relative_to(script.parents[2])}`\n{r.stdout}{r.stderr}"
    )


# ── o `done` que não cabia no orçamento (job `5ab98119`) ────────────────────


#: O `done` real do job 5ab98119, cortado onde o teto de 700 tokens o cortou.
_DONE_CORTADO = (
    '{"thought": "Já tenho 4 produtos anotados.", "action": "done", '
    '"answer": "1) SanDisk Cruzer Blade 64GB - R$ 65,76 - Frete GRÁTIS, entrega terça'
)


def test_done_cortado_e_pescado_em_vez_de_recusado():
    """O corte não pode custar um job que deu certo.

    O job 5ab98119 coletou 4 produtos com preço, decidiu `done` e escreveu a
    resposta inteira dentro do `answer` — ~1.650 caracteres, acima do teto de
    700 tokens da decisão. O JSON foi cortado no meio da string e a recusa
    devolveu a mensagem do parser de Python; o modelo reemitiu a MESMA resposta
    longa quatro vezes e o job morreu na regra das quatro recusas, com os 4
    produtos já no caderno.

    Defeito com o sinal invertido: quanto MELHOR a tarefa tinha ido, mais certo
    era o passo final falhar. O `answer` é o último campo do JSON e o único que
    cresce com a tarefa, então o corte cai nele e a INTENÇÃO de terminar chega
    inteira — é ela que se pesca.
    """
    decision = BrowserLocalDriver._parse(_DONE_CORTADO)
    assert decision["action"] == "done"
    assert decision["_cortado"] is True


def test_fragmento_do_answer_cortado_e_descartado():
    """Meia frase como linha de fecho é PIOR que nenhuma.

    "…Blade 64GB - R$ 65,76 - Frete GRÁTIS, entrega terça" termina num ponto
    arbitrário: deixar o fragmento entrar no resumo entrega ao usuário um preço
    que pode estar truncado. O conteúdo vem do caderno; do texto salvo vem só a
    decisão de encerrar.
    """
    decision = BrowserLocalDriver._parse(_DONE_CORTADO)
    assert decision["args"]["answer"] == ""
    assert "SanDisk" not in json.dumps(decision, ensure_ascii=False)


def test_acao_que_nao_e_done_continua_recusada_quando_cortada():
    """Só o `done` é pescado: um `click` cortado pode ter perdido o `ref`."""
    with pytest.raises(ValueError) as erro:
        BrowserLocalDriver._parse('{"action": "click", "args": {"re')
    msg = str(erro.value)
    assert "CORTADA por tamanho" in msg
    assert "caderno" in msg           # aponta a saída
    assert "Unterminated" not in msg  # e não a mensagem do parser


def test_finish_reason_manda_mais_que_a_cauda_do_texto():
    """`finish_reason` é o sinal de verdade; a cauda é só heurística de reserva.

    Uma resposta cortada EXATAMENTE depois de uma chave fechada passa pela
    heurística como se estivesse completa. O provider sabe que não está.
    """
    quase = '{"thought": "pronto", "action": "done", "args": {"answer": "ok"}'
    # Pela cauda, isto não termina em `}` → corte. Com o provider dizendo que
    # terminou sozinho, é malformado de verdade e a mensagem muda.
    with pytest.raises(ValueError) as erro:
        BrowserLocalDriver._parse(quase, cortado=False)
    assert "CORTADA por tamanho" not in str(erro.value)


def test_ausencia_de_finish_reason_e_nao_sei_e_nao_nao_foi_cortado():
    """Provider que não reporta cai na heurística, não no silêncio otimista."""

    class SemCampo:
        pass

    class Comprido:
        finish_reason = "length"

    class Terminou:
        finish_reason = "stop"

    assert BrowserLocalDriver._foi_cortado(SemCampo()) is None
    assert BrowserLocalDriver._foi_cortado(Comprido()) is True
    assert BrowserLocalDriver._foi_cortado(Terminou()) is False


def test_json_invalido_mas_completo_continua_erro_de_parser():
    """Resposta completa e malformada é outro caso — não confundir os dois."""
    with pytest.raises(ValueError) as erro:
        BrowserLocalDriver._parse('{"action": "done", "answer": pendrive}')
    assert "CORTADA por tamanho" not in str(erro.value)


# ── quem é o DONO da resposta final ────────────────────────────────────────


def _driver_nu() -> BrowserLocalDriver:
    """Driver sem device e sem modelo: basta para os métodos puros do fecho."""

    async def _act(action, args):  # pragma: no cover - nunca chamado aqui
        raise AssertionError(f"nenhum passo devia sair: {action}")

    return BrowserLocalDriver(act=_act, llm=None, model="texto")


def test_done_sem_answer_fecha_quando_o_caderno_tem_conteudo():
    """O `answer` é fecho, não entrega — e o `_com_caderno` entrega o caderno.

    Exigir o campo era exigir uma SEGUNDA cópia da resposta, e era essa cópia
    que estourava o teto (job 5ab98119). Pior: a prosa do `answer` é reescrita
    de novo pelo `_compose` do dispatcher uma camada acima.
    """
    driver = _driver_nu()
    driver.notes = ["pen drive SanDisk 64GB — R$ 65,76, frete grátis"]
    action, args = driver._validate({"action": "done", "args": {}}, {"elements": []})
    assert action == "done"
    assert args.get("answer", "") == ""


def test_done_sem_answer_e_sem_caderno_continua_recusado():
    """Caso de 2026-09-02: sem caderno e sem texto não há nada para entregar.

    A mesma condicional resolve as duas falhas, de sinais opostos — e é por isso
    que ela existe em vez de simplesmente afrouxar o campo.
    """
    driver = _driver_nu()
    assert driver.notes == []
    with pytest.raises(ValueError) as erro:
        driver._validate({"action": "done", "args": {}}, {"elements": []})
    msg = str(erro.value)
    assert "CADERNO VAZIO" in msg
    assert "note" in msg  # diz o que fazer


def test_a_recusa_nao_ensina_mais_que_o_answer_e_a_resposta():
    """Era a única frase do sistema que ensinava o contrário do prompt.

    O modelo lê a recusa justamente quando está mais disposto a mudar de
    comportamento, então ela não pode dizer "o `answer` É a resposta que o
    usuário vai ler" enquanto o prompt diz que quem entrega é o caderno.
    """
    driver = _driver_nu()
    with pytest.raises(ValueError) as erro:
        driver._validate({"action": "done", "args": {}}, {"elements": []})
    assert "É a resposta" not in str(erro.value)


def test_fecho_sem_texto_diz_quanto_tem_e_nao_resume_o_caderno():
    """Resumir aqui seria a TERCEIRA cópia da mesma resposta."""
    driver = _driver_nu()
    driver.notes = ["item A — R$ 10", "item B — R$ 20"]
    fecho = driver._fecho_do_caderno()
    assert "2 itens" in fecho
    assert "item A" not in fecho
    assert driver._fecho_do_caderno() == fecho  # estável
    vazio = _driver_nu()
    assert vazio._fecho_do_caderno() == "Terminei a tarefa."


def test_rotulo_do_caderno_nao_diz_ate_parar_num_job_concluido():
    """Agora que o `done` fecha com uma linha curta, este é o caminho NORMAL.

    Antes a linha quase nunca aparecia num job bem-sucedido, porque o `answer`
    trazia a lista e o dedup zerava o que faltava. Dizer "até parar" no caso de
    sucesso seria mentir sobre o desfecho num rótulo que o usuário lê.
    """
    driver = _driver_nu()
    driver.notes = ["pen drive SanDisk 64GB — R$ 65,76"]
    bom = driver._com_caderno("Terminei a tarefa.", "completed")
    assert "até parar" not in bom
    assert "SanDisk" in bom
    ruim = driver._com_caderno("Parei porque você apertou PARE.", "failed")
    assert "até parar" in ruim
    assert "SanDisk" in ruim


@pytest.mark.asyncio
async def test_run_guarda_o_fecho_sem_o_caderno():
    """O contrato que a entrega em duas metades consome.

    `summary` leva tudo (é o que o cérebro lê e o que vai para o caderno do
    servidor); `fecho` leva só a linha de prosa. O runner usa um para o
    `result_summary` e o outro para o `extra_metadata["entrega"]`, e é essa
    separação que impede o redator do dispatcher de parafrasear preço.
    """

    class _Resp:
        def __init__(self, c):
            self._c = c

        def get_content(self):
            return self._c

    class _Prov:
        def __init__(self, passos):
            self.passos = list(passos)

        async def complete(self, **kw):
            return _Resp(json.dumps(self.passos.pop(0)))

    class _LLM:
        def __init__(self, p):
            self._p = p

        def get_provider_for_model(self, _m):
            return self._p

    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 1}
        if action == "browser.snapshot":
            return {
                "url": "https://exemplo.com", "title": "x", "ready": "complete",
                "stop": False, "elements": [], "text": "x",
            }
        return {"ok": True}

    passos = [
        {"action": "note", "args": {"texto": "pen drive 64GB — R$ 65,76"}},
        {"action": "done", "args": {"answer": "Achei o que você pediu."}},
    ]
    driver = BrowserLocalDriver(
        act=act, llm=_LLM(_Prov(passos)), model="fake", vision_model=None
    )
    outcome = await driver.run("procura pen drive")

    assert outcome.fecho == "Achei o que você pediu."
    assert "65,76" not in outcome.fecho          # o fato não está na prosa
    assert "65,76" in outcome.summary            # mas está no resumo inteiro
    assert outcome.notes == ["pen drive 64GB — R$ 65,76"]


def test_caderno_inteiro_chega_quando_o_answer_veio_vazio():
    """O caminho completo do corte: pescado, fechado, e nada perdido."""
    driver = _driver_nu()
    driver.notes = [f"produto {n} — R$ {n}0,00" for n in range(1, 5)]
    decision = BrowserLocalDriver._parse(_DONE_CORTADO)
    action, args = driver._validate(decision, {"elements": []})
    assert action == "done"
    entrega = driver._com_caderno(
        args.get("answer") or driver._fecho_do_caderno(), "completed"
    )
    for n in range(1, 5):
        assert f"produto {n}" in entrega


def test_done_cabe_no_orcamento_de_uma_tarefa_rica():
    """SEGUNDA linha de defesa: o teto acomoda o modelo que recopia mesmo assim.

    A primeira é não pedir a cópia (`_ANSWER_MAX_CHARS`, `_validate`); a
    terceira é a pescaria do `_parse`. O `answer` real do job 5ab98119 tinha
    ~1.650 caracteres e o teto antigo (700) mal cobria isso somado ao pensamento
    e ao andaime do JSON.
    """
    from core.browser_local.driver import _ANSWER_MAX_CHARS, _DECISAO_MAX_TOKENS

    assert _DECISAO_MAX_TOKENS >= 1500
    # O pedido tem de caber FOLGADO no teto, como o `_words_for` da pesquisa:
    # ~2,1 caracteres por token em pt-BR, e o resto do orçamento é do `thought`.
    assert _ANSWER_MAX_CHARS * 3 < _DECISAO_MAX_TOKENS * 2.1
