"""O ciclo lembra do que ele mesmo disse, e não morre num erro na cara do usuário.

Dois defeitos medidos na session-389114d4 (2026-09-23), interação 29 — input "tem o
mais recente do INSS que foi um rombo ainda maior":

1. **Amnésia.** `history` vinha de um `get_history` único, feito antes do turno, e
   era repassado idêntico a todos os passos. O payload do passo 2 tinha
   ``[50] user "tem o mais recente do INSS…"`` seguido de ``[51] user "levantar o
   histórico completo…"`` e NENHUMA `assistant` no meio. Duas `user` seguidas leem
   como pedido novo: as quatro rodadas abriram com a mesma frase ("Você tem razão —
   e esse é o caso mais grave que eu deixei de fora") e o chat ganhou quatro bolhas
   quase idênticas.
2. **Morte por guarda.** Repetição/orçamento/profundidade descartavam o passo, o
   turno terminava sem `is_final`, e o fechamento forçado mandava uma string fixa:
   "Parei aqui: o próximo passo seria repetir um que já rodou neste turno." Duas
   vezes na mesma sessão — mecânica interna, sobre um "passo" que o usuário nunca
   pediu, no lugar da resposta.
"""

from uuid import uuid4

import pytest

from core.pipeline.orchestrator import PipelineOrchestrator
from core.pipeline.types import Handoff, PipelineStep, StepOutcome
from shared.types import IntentType

USER_MSG = "tem o mais recente do INSS que foi um rombo ainda maior"
ORDEM = (
    "levantar o histórico completo dos casos do Charlie e Bravo com status "
    "processual para fechar a camada 5"
)


def _kind_da_instrucao(texto: str | None) -> str | None:
    """Qual correção o passo recebeu, lida do texto que chegou ao prompt.

    Casar contra o dicionário real em vez de procurar a frase: se a instrução for
    reescrita, o teste acompanha em vez de passar por engano.
    """
    if not texto:
        return None
    from modules.desktop.ai_service import _CYCLE_CORRECTION_INSTRUCTION

    for kind, molde in _CYCLE_CORRECTION_INSTRUCTION.items():
        if texto.startswith(molde.split("\n", 1)[0]):
            return kind
    raise AssertionError(f"instrução de correção desconhecida: {texto[:60]!r}")


def _self(text: str) -> Handoff:
    return Handoff(target_intent=IntentType.DIRECT_RESPONSE, input_text=text, is_self=True)


def _svc(**overrides):
    """AIService cru com só o que `_run_pipeline` lê."""
    from modules.desktop.ai_service import AIService

    svc = object.__new__(AIService)

    class _S:
        pipeline_handoff_enabled = True
        pipeline_self_handoff_enabled = True
        pipeline_self_handoff_max_steps = 8
        pipeline_self_handoff_max_depth = 7
        pipeline_cascade_enabled = False

    for k, v in overrides.items():
        setattr(_S, k, v)
    svc.settings = _S()
    return svc


def _decision():
    from shared.types import RoutingDecision

    return RoutingDecision(
        intent=IntentType.DIRECT_RESPONSE, confidence=0.95, needs_memory=True,
        needs_plan=False, explanation="teste",
    )


async def _drive(svc, roteiro, history=None):
    """Roda `_run_pipeline` com o handler cognitivo roteirizado.

    Devolve (respostas, chamadas), com cada chamada guardando uma CÓPIA do
    histórico — a lista é mutada de propósito, e guardar a referência faria todos
    os snapshots parecerem iguais no fim.
    """
    from modules.desktop.ai_service import AIServiceResponse

    sid = uuid4()
    calls: list[dict] = []

    def _fake(_sid, msg, hist, *a, **kw):
        plano = roteiro[min(len(calls), len(roteiro) - 1)]
        instrucao = (kw.get("metadata") or {}).get("pipeline_cycle_correction")
        calls.append({
            "msg": msg,
            "history": list(hist or []),
            "correction": instrucao,
            "kind": _kind_da_instrucao(instrucao),
            "work_note": (kw.get("metadata") or {}).get("pipeline_work_note"),
            "enable_handoff": kw.get("enable_handoff", True),
        })
        handoffs = (plano.get("handoffs") or []) if kw.get("enable_handoff", True) else []

        async def _gen():
            yield AIServiceResponse(
                intent=IntentType.DIRECT_RESPONSE, session_id=_sid,
                message=plano["reply"], is_final=not handoffs, iterations=1,
                metadata={"persist_reply": True} if handoffs else None,
            )
            if handoffs:
                yield AIServiceResponse(
                    intent=IntentType.DIRECT_RESPONSE, session_id=_sid, message=None,
                    is_final=False,
                    metadata={
                        "__pipeline_handoffs__": handoffs,
                        "__pipeline_work__": {
                            "reply": plano["reply"],
                            "tool_trace": plano.get("tool_trace") or [],
                            "files": [],
                        },
                    },
                )

        return _gen()

    svc._handle_direct_response = _fake
    out = [
        r async for r in svc._run_pipeline(
            decision=_decision(), user_message=USER_MSG,
            history=history if history is not None else [{"role": "user", "content": USER_MSG}],
            sid=sid, trace=None, sys_context=None, slog=None, user_id=None,
            metadata=None, saved_msg=None, allow_clarification=False,
            clarification_round=1,
        )
    ]
    return out, calls


# ------------------------------------------------------------------ amnésia


@pytest.mark.asyncio
async def test_a_resposta_do_passo_entra_no_historico_do_passo_seguinte():
    """Sem isto o passo 2 vê duas `user` seguidas e responde do zero."""
    svc = _svc()
    _, calls = await _drive(svc, [
        {"reply": "Você tem razão — o INSS é o caso mais grave.",
         "handoffs": [_self(ORDEM)], "tool_trace": [{"tool": "web_search", "ok": True}]},
        {"reply": "Levantei: o quadro fechado está aqui."},
    ])

    assert [c["msg"] for c in calls] == [USER_MSG, ORDEM]

    # O passo 1 recebeu o histórico como veio do banco.
    assert calls[0]["history"] == [{"role": "user", "content": USER_MSG}]

    # O passo 2 recebeu a fala do passo 1 ANTES da ordem — era exatamente isso que
    # faltava. A ordem entra como `user` porque é assim que o montador do payload
    # já a apresenta; quem avisa que não é fala do usuário é o bloco "O que você já
    # fez neste turno", que viaja no mesmo prompt (`pipeline_work_note`).
    assert calls[1]["history"] == [
        {"role": "user", "content": USER_MSG},
        {"role": "assistant", "content": "Você tem razão — o INSS é o caso mais grave."},
    ]
    assert calls[1]["work_note"] is not None


@pytest.mark.asyncio
async def test_a_fala_do_passo_entra_truncada_e_nenhum_passo_desaparece():
    """Teto por resposta, sem janela deslizante.

    Manter só as últimas N respostas recriaria o bug de 2026-09-18 (janela do
    prompt menor que o teto de rodadas), que é o que fez o cérebro voltar a pedir
    o que já tinha pedido enquanto a guarda o matava por isso.
    """
    from modules.desktop.ai_service import _CYCLE_REPLY_CHARS

    svc = _svc()
    longa = "x" * (_CYCLE_REPLY_CHARS + 500)
    _, calls = await _drive(svc, [
        {"reply": longa, "handoffs": [_self("primeira ordem distinta")],
         "tool_trace": [{"tool": "web_search", "ok": True}]},
        {"reply": longa, "handoffs": [_self("segunda ordem sem parentesco nenhum")],
         "tool_trace": [{"tool": "neo_bash", "ok": True}]},
        {"reply": "fechado"},
    ])

    falas = [m for m in calls[-1]["history"] if m["role"] == "assistant"]
    assert len(falas) == 2, "as duas respostas continuam lá — nada de janela"
    assert all(len(m["content"]) == _CYCLE_REPLY_CHARS for m in falas)


@pytest.mark.asyncio
async def test_turno_de_um_passo_nao_mexe_no_historico():
    """Regressão: o caminho comum tem que ficar idêntico ao de antes."""
    svc = _svc()
    original = [{"role": "user", "content": USER_MSG}]
    _, calls = await _drive(svc, [{"reply": "resposta direta"}], history=original)

    assert len(calls) == 1
    assert calls[0]["history"] == original == [{"role": "user", "content": USER_MSG}]


# -------------------------------------------------------------- autocorreção


@pytest.mark.asyncio
async def test_dois_passos_sem_executar_nada_fecham_o_turno_falando():
    """O defeito da interação 29, reproduzido: ciclo reescrevendo a resposta.

    Quatro rodadas, zero tool. Agora a segunda volta sem ação fecha o turno, e o
    fecho é escrito pelo cérebro — nunca a string fixa do sistema.
    """
    from modules.desktop.ai_service import _CYCLE_CLOSING_MESSAGE

    svc = _svc()
    out, calls = await _drive(svc, [
        # Cada passo promete e não executa, com ordem NOVA a cada volta (para o
        # corte vir da falta de ação, não da repetição).
        {"reply": "Você tem razão. Vou levantar o histórico.",
         "handoffs": [_self("levantar o histórico dos dois lados com status")]},
        {"reply": "Você tem razão. Disparando a pesquisa agora.",
         "handoffs": [_self("apurar rachadinhas e chocolate no MPRJ")]},
        {"reply": "Você tem razão. Vou fechar a camada 5.",
         "handoffs": [_self("classificar cada caso em provado, anulado ou em curso")]},
        {"reply": "Fecho com o que tenho: nenhum dos dois casos foi apurado por fonte."},
    ])

    # raiz + 2 continuações ociosas + fechamento. A 4ª volta não acontece.
    assert [c["kind"] for c in calls] == [None, None, None, "close"]
    assert calls[-1]["msg"] == "fechar o turno com o que já foi levantado"
    assert calls[-1]["enable_handoff"] is False, (
        "com handoff ligado o verificador de promessa leria o próprio fecho como "
        "promessa e abriria outro ciclo — o turno não terminaria"
    )
    assert "FECHE O TURNO AGORA" in calls[-1]["correction"]

    final = [r for r in out if r.is_final]
    assert len(final) == 1
    assert final[0].message == (
        "Fecho com o que tenho: nenhum dos dois casos foi apurado por fonte."
    )
    assert final[0].message not in _CYCLE_CLOSING_MESSAGE.values()


@pytest.mark.asyncio
async def test_ordem_parafraseada_leva_empurrao_antes_de_fechar():
    """A ORDEM_2 real da interação 29: 0,80 de cobertura sobre a ORDEM_1."""
    svc = _svc()
    ordem_2 = (
        "levantar o histórico completo dos casos do Charlie e Bravo com status "
        "processual real e fonte"
    )
    out, calls = await _drive(svc, [
        {"reply": "primeira volta", "handoffs": [_self(ORDEM)],
         "tool_trace": [{"tool": "web_search", "ok": True}]},
        {"reply": "segunda volta", "handoffs": [_self(ordem_2)],
         "tool_trace": [{"tool": "web_search", "ok": True}]},
        # O empurrão chega aqui: o modelo obedece e entrega.
        {"reply": "Fechado: o quadro está montado com fonte em cada caso."},
    ])

    assert [c["kind"] for c in calls] == [None, None, "retry"]
    empurrao = calls[-1]["correction"]
    assert "ESSA ORDEM NÃO RODOU" in empurrao
    # A instrução diz ONDE a resposta já está — recusar sem explicar é o que fazia
    # o cérebro tentar de novo do mesmo jeito.
    assert ORDEM[:40] in empurrao
    # E o empurrão mantém a ordem original: o que muda é a instrução, não o pedido.
    assert calls[-1]["msg"] == ordem_2
    assert [r for r in out if r.is_final][0].message.startswith("Fechado:")


@pytest.mark.asyncio
async def test_o_empurrao_que_nao_resolve_vira_fechamento():
    """Segunda volta no mesmo ponto: fecha, sempre falando."""
    from modules.desktop.ai_service import _CYCLE_CLOSING_MESSAGE

    svc = _svc()
    out, calls = await _drive(svc, [
        {"reply": "r1", "handoffs": [_self(ORDEM)],
         "tool_trace": [{"tool": "web_search", "ok": True}]},
        # Sempre a MESMA ordem: empurrão, e depois fecho.
        {"reply": "r2", "handoffs": [_self(ORDEM)],
         "tool_trace": [{"tool": "web_search", "ok": True}]},
    ])

    assert [c["kind"] for c in calls] == [None, None, "retry", "close"]
    final = [r for r in out if r.is_final]
    assert len(final) == 1
    assert final[0].message not in _CYCLE_CLOSING_MESSAGE.values()


@pytest.mark.asyncio
async def test_ciclo_que_trabalha_nao_e_interrompido():
    """O "não atrapalhar": passo que executa e pede coisa nova segue livre."""
    svc = _svc()
    out, calls = await _drive(svc, [
        {"reply": "baixei a página", "handoffs": [_self("extrair a tabela do HTML")],
         "tool_trace": [{"tool": "web_fetch", "ok": True}]},
        {"reply": "extraí a tabela", "handoffs": [_self("gerar o gráfico em png")],
         "tool_trace": [{"tool": "neo_bash", "ok": True}]},
        {"reply": "entreguei o gráfico", "tool_trace": [{"tool": "file_save", "ok": True}]},
    ])

    assert [c["kind"] for c in calls] == [None, None, None]
    assert [r for r in out if r.is_final][0].message == "entreguei o gráfico"


def test_passo_de_fechamento_herda_a_profundidade_do_pai():
    """Com `+1` a própria guarda mataria o passo que existe para não morrer."""
    pai = PipelineStep(
        intent=IntentType.DIRECT_RESPONSE, input_text="x", origin="self", depth=7
    )
    fecho = PipelineStep.self_correction(pai, kind="close")

    assert fecho.depth == 7
    assert fecho.origin == "self"
    assert fecho.correction == "close"
    assert fecho.is_self_continuation is True


@pytest.mark.asyncio
async def test_cascata_do_classificador_nao_virou_ciclo_de_correcao():
    """A comparação por conteúdo vale só no ciclo.

    Uma cascata declarada pelo classificador tem passos parecidos de propósito
    ("buscar os links", "baixar os links"); aplicar cobertura ali transformaria
    cascata em correção — mudança de comportamento que ninguém pediu.
    """
    notices: list[tuple[str, str]] = []
    vistos: list[str] = []

    async def runner(step):
        vistos.append(step.input_text)
        yield "r", None
        yield None, StepOutcome(ok=True)

    orch = PipelineOrchestrator(
        runner, on_notice=lambda k, m: notices.append((k, m))
    )
    passos = [
        PipelineStep(intent=IntentType.DIRECT_RESPONSE, input_text=t, origin="classifier")
        for t in ("buscar os links dos editais no portal", "baixar os links dos editais do portal")
    ]
    [r async for r in orch.run(passos)]

    assert vistos == [p.input_text for p in passos], "os dois passos da cascata rodaram"
    assert notices == []
