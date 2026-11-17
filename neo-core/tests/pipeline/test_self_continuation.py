"""Self-continuation — the cognitive core handing off to ITSELF.

The failure this exists to remove, measured in session-fd467d08: four turns in a
row ended with "sigo tentando"/"vou resolver" and no work followed, because a turn
ends when the tool loop returns text. These tests pin the three properties that
make the cycle safe to run unattended: it advances on real results, it stops, and
it is never silent about stopping.
"""

import pytest

from core.pipeline.handoff_parser import extract_handoffs
from core.pipeline.orchestrator import PipelineOrchestrator
from core.pipeline.policy import handoff_requires_confirmation
from core.pipeline.types import Handoff, PipelineStep, StepOutcome
from shared.types import IntentType


def _step(intent=IntentType.DIRECT_RESPONSE, text="oi", origin="classifier", depth=0):
    return PipelineStep(intent=intent, input_text=text, origin=origin, depth=depth)


def _self(text: str) -> Handoff:
    return Handoff(target_intent=IntentType.DIRECT_RESPONSE, input_text=text, is_self=True)


# ---------------------------------------------------------------- parser


def test_continuar_block_targets_the_cognitive_core_itself():
    text = "Vou baixar a imagem.\n[[HANDOFF:continuar]]baixar a imagem com curl -L[[/HANDOFF]]"
    clean, handoffs = extract_handoffs(text)

    assert clean == "Vou baixar a imagem."
    assert len(handoffs) == 1
    h = handoffs[0]
    assert h.is_self is True
    assert h.target_intent == IntentType.DIRECT_RESPONSE
    assert h.target_skill is None       # not a skill — must not be routed as one
    assert h.input_text == "baixar a imagem com curl -L"


def test_self_block_is_stripped_but_dropped_when_disabled():
    """Feature off → no chaining, and the directive still never leaks to the user."""
    text = "Beleza.\n[[HANDOFF:continuar]]seguir tentando[[/HANDOFF]]"
    clean, handoffs = extract_handoffs(text, allow_self=False)

    assert handoffs == []
    assert "HANDOFF" not in clean
    assert clean == "Beleza."


def test_skill_handoffs_are_unaffected_by_the_self_target():
    clean, handoffs = extract_handoffs(
        "Ok.\n[[HANDOFF:home_assistant]]ligar a luz da sala[[/HANDOFF]]"
    )
    assert len(handoffs) == 1
    assert handoffs[0].is_self is False
    assert handoffs[0].target_skill == "home_assistant"
    assert clean == "Ok."


# ---------------------------------------------------------------- policy


def test_self_continuation_does_not_require_confirmation():
    """Gating every iteration would defeat the feature; the sandbox is reversible."""
    assert handoff_requires_confirmation(IntentType.DIRECT_RESPONSE) is False
    # The outward target keeps its gate.
    assert handoff_requires_confirmation(IntentType.WHATSAPP_MESSAGING) is True


# ---------------------------------------------------------------- step typing


def test_self_handoff_produces_a_self_origin_step_carrying_the_note():
    parent = _step()
    nxt = PipelineStep.from_handoff(_self("baixar imagem"), parent=parent, context_note="passo 1: ok")

    assert nxt.origin == "self"
    assert nxt.is_self_continuation is True
    assert nxt.context_note == "passo 1: ok"
    assert nxt.depth == 1


def test_skill_handoff_never_inherits_the_note():
    """A skill runs isolated — it must not see this conversation's work log."""
    parent = _step()
    ha = Handoff(target_intent=IntentType.HOME_ASSISTANT, input_text="ligar luz")
    nxt = PipelineStep.from_handoff(ha, parent=parent, context_note="passo 1: ok")

    assert nxt.origin == "handoff"
    assert nxt.is_self_continuation is False
    assert nxt.context_note is None


# ---------------------------------------------------------------- orchestrator


@pytest.mark.asyncio
async def test_cycle_runs_until_the_model_stops_asking():
    """Three continuations then a terminal step — the whole point of the feature."""
    seen: list[str] = []

    async def runner(step):
        seen.append(step.input_text)
        n = len(seen)
        yield f"r{n}", None
        if n < 4:
            yield None, StepOutcome(
                ok=True, handoffs=[_self(f"tentativa {n + 1}")],
                work_note=f"passo {n}: falhou",
                # Tentativa que EXECUTOU e falhou é progresso — o ciclo existe para
                # isso. Sem `acted`, três rodadas de puro texto são cortadas pela
                # guarda de passo ocioso, que é outro teste.
                acted=True,
            )
        else:
            yield None, StepOutcome(ok=True, final_text="pronto")

    orch = PipelineOrchestrator(runner, self_max_steps=8, self_max_depth=7)
    out = [r async for r in orch.run([_step(text="raiz")])]

    assert out == ["r1", "r2", "r3", "r4"]
    assert seen == ["raiz", "tentativa 2", "tentativa 3", "tentativa 4"]


@pytest.mark.asyncio
async def test_continuation_receives_the_accumulated_work_notes():
    """Each continuation must see what really happened, or it retries blind."""
    notes: list[str | None] = []

    async def runner(step):
        notes.append(step.context_note)
        n = len(notes)
        yield f"r{n}", None
        if n < 3:
            yield None, StepOutcome(
                ok=True, handoffs=[_self(f"passo {n + 1}")], work_note=f"passo {n}: escreveu index.html"
            )
        else:
            yield None, StepOutcome(ok=True, final_text="fim")

    orch = PipelineOrchestrator(runner, self_max_steps=8, self_max_depth=7)
    [r async for r in orch.run([_step(text="raiz")])]

    assert notes[0] is None                                   # root has no history
    assert notes[1] == "passo 1: escreveu index.html"
    # Notes accumulate, so step 3 sees BOTH earlier steps, not just the last one.
    assert notes[2] == "passo 1: escreveu index.html\npasso 2: escreveu index.html"


@pytest.mark.asyncio
async def test_cycle_gets_the_larger_budget_not_the_skill_one():
    """A retry cycle burns steps fast; MAX_STEPS=4 would cut it at 3 attempts.

    ``acted=True`` em cada passo não é detalhe: desde a guarda de passo ocioso, um
    ciclo que só produz texto é cortado em ``MAX_IDLE_CYCLE_STEPS`` (é o defeito da
    session-389114d4). O orçamento grande existe para o ciclo que EXECUTA.
    """
    runs = 0

    async def runner(step):
        nonlocal runs
        runs += 1
        n = runs
        yield f"r{n}", None
        yield None, StepOutcome(
            ok=True, handoffs=[_self(f"tentativa {n + 1}")], acted=True
        )

    orch = PipelineOrchestrator(runner, self_max_steps=8, self_max_depth=7)
    out = [r async for r in orch.run([_step(text="raiz")])]

    # 8 passos de trabalho + o passo de fechamento que o estouro de orçamento
    # enfileira (é ele que faz o turno terminar falando, não com string fixa).
    assert len(out) == 9
    assert PipelineOrchestrator.MAX_STEPS == 4   # the ordinary chain is untouched


@pytest.mark.asyncio
async def test_budget_exhaustion_reports_a_notice():
    """Silence here is the bug: the turn would end with no way to know why.

    E o aviso não pode ser a ÚNICA coisa que acontece: estourar orçamento
    enfileira o passo de fechamento, que é quem escreve a resposta final. Sem ele,
    o turno acaba sem `is_final` e o usuário recebe a string fixa do sistema.
    """
    notices: list[tuple[str, str]] = []
    vistos: list[str | None] = []

    async def runner(step):
        vistos.append(step.correction)
        yield "r", None
        yield None, StepOutcome(
            ok=True, handoffs=[_self(f"passo {step.depth + 2}")], acted=True
        )

    orch = PipelineOrchestrator(
        runner, self_max_steps=3, self_max_depth=7, on_notice=lambda k, m: notices.append((k, m))
    )
    [r async for r in orch.run([_step(text="raiz")])]

    assert [k for k, _ in notices] == ["budget"]
    assert "orçamento" in notices[0][1]
    # Orçamento efetivo é 4, não 3: o orquestrador ergue o teto do ciclo ao piso
    # do handoff comum (`max(self_max_steps, MAX_STEPS)`).
    assert vistos == [None, None, None, None, "close"]


@pytest.mark.asyncio
async def test_repeating_the_same_order_corrects_instead_of_dying():
    """Modelo repete a ordem → empurrão, depois fecho. Nunca morte calada.

    Antes isto descartava o passo e o turno terminava sem `is_final`, o que fazia
    o fechamento forçado cuspir "Parei aqui: o próximo passo seria repetir um que
    já rodou neste turno" — um erro de mecânica interna no lugar da resposta. O
    aviso continua saindo (auditoria), mas quem fala com o usuário é o cérebro.
    """
    notices: list[tuple[str, str]] = []
    vistos: list[str | None] = []

    async def runner(step):
        vistos.append(step.correction)
        yield f"r{len(vistos)}", None
        yield None, StepOutcome(
            ok=True, handoffs=[_self("baixar a imagem")], acted=True
        )

    orch = PipelineOrchestrator(
        runner, self_max_steps=8, self_max_depth=7, on_notice=lambda k, m: notices.append((k, m))
    )
    out = [r async for r in orch.run([_step(text="baixar a imagem")])]

    # raiz → (repete) retry → (repete) close. Termina, e termina rodando.
    assert vistos == [None, "retry", "close"]
    assert out == ["r1", "r2", "r3"]
    assert [k for k, _ in notices] == ["anti_loop", "anti_loop"]


@pytest.mark.asyncio
async def test_depth_guard_reports_before_the_budget_would():
    notices: list[tuple[str, str]] = []

    vistos: list[str | None] = []

    async def runner(step):
        vistos.append(step.correction)
        yield "r", None
        yield None, StepOutcome(
            ok=True, handoffs=[_self(f"passo {step.depth + 2}")], acted=True
        )

    orch = PipelineOrchestrator(
        runner, self_max_steps=20, self_max_depth=2, on_notice=lambda k, m: notices.append((k, m))
    )
    [r async for r in orch.run([_step(text="raiz")])]

    assert [k for k, _ in notices] == ["max_depth"]
    # O passo de fechamento herda a profundidade do pai, não `+1`: com `+1` a
    # própria guarda mataria o passo que existe para o turno não morrer.
    assert vistos[-1] == "close"


# ---------------------------------------------------------------- promise gate


@pytest.mark.parametrize(
    "reply",
    [
        "Vou procurar uma imagem de domínio público e incluir.",
        "Sigo por minha conta, beleza?",
        "Já te mando o zip assim que fechar.",
        "Deixa comigo, resolvo isso agora.",
        "Vou tentar outra fonte de imagem.",
        # The exact line that slipped through on the first E2E run.
        "Perfeito — 4 pastas com wallpapers. Vou ver os nomes reais e baixar os melhores pro site.",
        # Both verbatim from later E2E runs: the reply announces the next move
        # without ever saying "vou", so the marker list alone let the turn end on
        # a promise and the backstop never fired.
        "Script Jikan pronto. Rodando o download.",
        "Fandom respondeu. Agora busco imagens dos outros personagens e baixo tudo.",
    ],
)
def test_promise_gate_catches_announced_work(reply):
    from modules.desktop.ai_service import _looks_like_promise

    assert _looks_like_promise(reply) is True


@pytest.mark.parametrize(
    "reply",
    [
        "Pronto, o zip está aí em cima com as 3 páginas.",
        "Você prefere imagens por link ou baixadas no arquivo?",
        "Não consigo fazer isso sem o token da API.",
        "O ar-condicionado do quarto está em 24°.",
        "",
        # A gerund only announces work when it CLOSES the reply; mid-sentence it
        # narrates what already happened and must stay free.
        "Rodei o script, gerando o zip. Tá tudo pronto, é só baixar.",
        # "baixo" as an adjective — the reason it is not a closing marker.
        "O preço ficou baixo demais pra ser verdade.",
    ],
)
def test_promise_gate_ignores_delivery_questions_and_chat(reply):
    """A false positive only costs a cheap call; these must stay free anyway."""
    from modules.desktop.ai_service import _looks_like_promise

    assert _looks_like_promise(reply) is False


def test_promise_gate_is_accent_insensitive():
    from modules.desktop.ai_service import _looks_like_promise

    assert _looks_like_promise("Ja te mando o arquivo") is True
    assert _looks_like_promise("Já te mando o arquivo") is True


@pytest.mark.asyncio
async def test_notice_sink_failure_never_breaks_the_turn():
    """Telemetry is not load-bearing."""
    def boom(kind, message):
        raise RuntimeError("sink down")

    async def runner(step):
        yield "r", None
        yield None, StepOutcome(ok=True, handoffs=[_self("mesma ordem")], acted=True)

    orch = PipelineOrchestrator(runner, self_max_steps=8, on_notice=boom)
    out = [r async for r in orch.run([_step(text="mesma ordem")])]

    # O que importa é terminar: a cadeia é finita mesmo com a telemetria caindo
    # em TODA guarda (raiz + retry + close, com o sink explodindo nas três).
    assert out == ["r", "r", "r"]


# ------------------------------------------------- contrato com o desktop

# O ciclo só é útil se o usuário VÊ ele girando. Três coisas viajam pro
# neo-desktop-tauri e nenhuma estava coberta: o aviso de passo executando, as
# respostas intermediárias como bolha própria, e o terminador do turno. Sem o
# último o desktop trava — ele só emite `complete` ao ver um is_final
# (ai_agent.rs), então uma cadeia cortada por guarda deixaria o chat girando.

def _svc_for_pipeline(**overrides):
    """AIService cru com só o que _run_pipeline lê. Mesmo molde de
    tests/test_cognitive_tool_loop.py — instanciar o serviço de verdade puxaria
    DB, LLM e PKS para testar controle de fluxo."""
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


async def _drive(svc, decision, scripted):
    """Roda _run_pipeline com _handle_direct_response roteirizado por passo."""
    from modules.desktop.ai_service import AIServiceResponse
    from shared.types import IntentType as IT
    from uuid import uuid4

    sid = uuid4()
    calls: list[str] = []

    def _fake_handler(_sid, msg, *a, **kw):
        calls.append(msg)
        plan = scripted[min(len(calls) - 1, len(scripted) - 1)]

        async def _gen():
            # Honrar `enable_handoff` não é detalhe do harness: é COMO o passo de
            # fechamento fecha. Ele roda com handoff desligado, então não emite
            # bloco, e sem handoff a resposta é `is_final` (handoff_defers_final).
            # Um harness que ignora isso devolve o fecho como não-final e produz
            # exatamente o sintoma que o fecho existe para evitar.
            handoffs = (plan.get("handoffs") or []) if kw.get("enable_handoff", True) else []
            yield AIServiceResponse(
                intent=IT.DIRECT_RESPONSE,
                session_id=_sid,
                message=plan["reply"],
                is_final=not handoffs,
                iterations=1,
                metadata={"persist_reply": True} if handoffs else None,
            )
            if handoffs:
                yield AIServiceResponse(
                    intent=IT.DIRECT_RESPONSE,
                    session_id=_sid,
                    message=None,
                    is_final=False,
                    metadata={
                        "__pipeline_handoffs__": handoffs,
                        # `tool_trace` não-vazio = o passo EXECUTOU. Sem isto a
                        # guarda de passo ocioso fecha o ciclo na 2ª volta e o
                        # teste de orçamento nunca chega a exercitar o orçamento.
                        "__pipeline_work__": {
                            "reply": plan["reply"],
                            "tool_trace": [{"tool": "neo_bash", "ok": True}],
                            "files": [],
                        },
                    },
                )

        return _gen()

    svc._handle_direct_response = _fake_handler
    out = []
    async for r in svc._run_pipeline(
        decision=decision, user_message="monta o site", history=[], sid=sid,
        trace=None, sys_context=None, slog=None, user_id=None, metadata=None,
        saved_msg=None, allow_clarification=False, clarification_round=0,
    ):
        out.append(r)
    return out, calls


def _decision():
    from shared.types import RoutingDecision, IntentType as IT

    return RoutingDecision(intent=IT.DIRECT_RESPONSE, confidence=1.0, needs_memory=False)


@pytest.mark.asyncio
async def test_desktop_sees_an_executing_marker_per_continuation():
    """Cada passo do ciclo se anuncia — é o que vira a bolha "Executando"."""
    from modules.desktop.ai_service import CYCLE_PROGRESS_MARKER

    svc = _svc_for_pipeline()
    out, calls = await _drive(svc, _decision(), [
        {"reply": "primeira tentativa falhou", "handoffs": [_self("baixar imagem com curl -A")]},
        {"reply": "pronto, zip entregue"},
    ])

    marked = [r for r in out if r.message and r.message.startswith(CYCLE_PROGRESS_MARKER)]
    assert len(marked) == 1, "a continuação tem que se anunciar ao desktop"
    assert "baixar imagem com curl -A" in marked[0].message
    assert marked[0].is_final is False, "o aviso de progresso não pode fechar o turno"
    assert calls == ["monta o site", "baixar imagem com curl -A"]


@pytest.mark.asyncio
async def test_intermediate_replies_persist_and_only_the_last_is_final():
    """As mensagens aparecem no chat conforme saem; só a última encerra."""
    svc = _svc_for_pipeline()
    out, _ = await _drive(svc, _decision(), [
        {"reply": "passo 1", "handoffs": [_self("passo 2 concreto")]},
        {"reply": "passo 2", "handoffs": [_self("passo 3 concreto")]},
        {"reply": "entreguei o zip"},
    ])

    from modules.desktop.ai_service import CYCLE_PROGRESS_MARKER
    falas = [r for r in out if r.message and not r.message.startswith(CYCLE_PROGRESS_MARKER)]
    assert [f.message for f in falas] == ["passo 1", "passo 2", "entreguei o zip"]
    # Intermediárias como bolha própria (persist_reply), não como "pensando".
    assert all((f.metadata or {}).get("persist_reply") for f in falas[:-1])
    assert [f.is_final for f in falas] == [False, False, True]


@pytest.mark.asyncio
async def test_a_cycle_cut_by_a_guard_still_terminates_the_turn():
    """Sem is_final o desktop nunca emite `complete` e o chat gira pra sempre.

    Orçamento 5 e não 2: o orquestrador ergue o teto do ciclo ao piso do handoff
    comum (``max(self_max_steps, MAX_STEPS)``), então um valor abaixo de 4 não
    vincula e o teste passaria sem exercitar guarda nenhuma.
    """
    svc = _svc_for_pipeline(pipeline_self_handoff_max_steps=5)
    # Ordens distintas de propósito: iguais, quem cortaria seria o anti-loop, e o
    # que se quer pinar aqui é o corte por orçamento.
    out, calls = await _drive(svc, _decision(), [
        {"reply": f"tentativa {i}", "handoffs": [_self(f"ordem concreta {i + 1}")]}
        for i in range(1, 7)
    ])

    from modules.desktop.ai_service import _CYCLE_CLOSING_MESSAGE

    # 5 passos de trabalho + o passo de fechamento que o estouro enfileira.
    assert len(calls) == 6, "o orçamento para a cadeia e manda fechar"
    assert calls[-1] == "fechar o turno com o que já foi levantado"
    assert out[-1].is_final is True, "a cadeia cortada tem que fechar o turno"
    # O fecho é FALA DO NEO, não string do sistema. Era isto que aparecia antes:
    # "Bati no limite de passos deste turno…" — mecânica interna na cara do
    # usuário, sobre um passo que ele nunca pediu.
    assert out[-1].message not in _CYCLE_CLOSING_MESSAGE.values()
    assert "limite de passos" not in out[-1].message


# ------------------------------------------- verificador vê o que foi despachado


class _CapturingProvider:
    """Provider falso que guarda o payload e responde o JSON combinado."""

    def __init__(self, raw: str):
        self._raw = raw
        self.payload: str = ""

    async def complete(self, *, messages, **kwargs):
        from types import SimpleNamespace
        self.payload = "\n".join(m.content for m in messages)
        # A seção de ações despachadas mora na mensagem do USUÁRIO; o system prompt
        # só fala dela em regra, então asserção sobre ele daria falso positivo.
        self.user_payload = "\n".join(
            m.content for m in messages if str(getattr(m.role, "value", m.role)) == "user"
        )
        return SimpleNamespace(get_content=lambda: self._raw)


class _FakeLLM:
    def __init__(self, provider):
        self._provider = provider

    def get_provider(self, name=None):
        return self._provider


def _service(provider):
    from modules.desktop.ai_service import AIService
    svc = object.__new__(AIService)
    svc.db = None          # get_classifier_config falha → cai no provider default
    svc.llm = _FakeLLM(provider)
    return svc


@pytest.mark.asyncio
async def test_dispatched_actions_reach_the_verifier_payload():
    """O verificador tem que SABER que o turno já despachou a ação.

    Regressão de prod (session-5102534a): a resposta foi "Vou pegar uma pedrada do
    teu mix." com [[HANDOFF:music]]tocar Slipknot - Duality junto. O bloco já sai
    do texto antes daqui, então o verificador via só a promessa, respondia
    promised=true e sintetizava "baixar a música do mix" — um download que ninguém
    pediu, em cima de uma ação que já estava rodando.
    """
    provider = _CapturingProvider('{"promised": false, "order": ""}')
    svc = _service(provider)

    synth = await svc._verify_promise_continuation(
        "uma musica que eu gosto",
        "Vou pegar uma pedrada do teu mix.",
        None,
        ["music: tocar Slipknot - Duality"],
    )

    assert synth is None
    assert "music: tocar Slipknot - Duality" in provider.user_payload
    assert "Ações já despachadas" in provider.user_payload


@pytest.mark.asyncio
async def test_promise_left_over_by_the_dispatch_still_continues():
    """Despachar uma ação não amordaça o backstop: o que sobra vira ordem."""
    provider = _CapturingProvider('{"promised": true, "order": "buscar a letra de Duality"}')
    svc = _service(provider)

    synth = await svc._verify_promise_continuation(
        "toca uma musica e me manda a letra",
        "Vou tocar e já te mando a letra.",
        None,
        ["music: tocar Slipknot - Duality"],
    )

    assert synth is not None and synth.is_self
    assert synth.input_text == "buscar a letra de Duality"


@pytest.mark.asyncio
async def test_no_dispatch_keeps_the_payload_clean():
    provider = _CapturingProvider('{"promised": true, "order": "baixar o arquivo"}')
    svc = _service(provider)

    synth = await svc._verify_promise_continuation("baixa aquilo", "Vou baixar agora.", None, None)

    assert synth is not None
    assert "Ações já despachadas" not in provider.payload, (
        "sem handoff, nem a regra nem a seção entram no prompt"
    )
