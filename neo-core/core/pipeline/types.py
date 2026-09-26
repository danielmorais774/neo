"""Core data types for the pipeline orchestrator.

These are deliberately plain dataclasses (no pydantic) — they live entirely inside
neo-core and never cross the gRPC boundary, so they carry no serialization concern.

- ``PipelineStep``  : one unit of work the orchestrator runs (an intent + its input).
- ``Handoff``       : a request, emitted at runtime by a step, to run another
                       pipeline next with a clean, self-contained input (Phase 2).
- ``StepOutcome``   : the result of running a step, including any handoffs it asked
                       for. In Phase 1 ``handoffs`` is always empty.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from shared.types import IntentType


def normalize_input(text: str) -> str:
    """Normalize an input for the anti-loop visited-set key.

    Lowercase, strip accents and collapse whitespace so that "Ligar  o AC" and
    "ligar o ac" map to the same key. Mirrors the accent handling used by the
    intent router, kept local to avoid importing the router here.
    """
    raw = (text or "").strip().lower()
    decomposed = unicodedata.normalize("NFKD", raw)
    no_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(no_accents.split())


# Palavras de função — saem antes de comparar duas ordens. Sem elas, duas ordens
# sobre assuntos diferentes já empatam em metade dos tokens só de artigo e
# preposição, e a guarda de quase-repetição viraria falso positivo constante.
_STOPWORDS = frozenset(
    """
    a as o os um uma uns umas de do da dos das e em no na nos nas por para com
    que ao aos ou se ja agora depois antes ate apos como cada todo toda todos
    todas isso isto esse essa este esta aquele aquela aquilo pelo pela pelos
    pelas sobre entre sem sob nao mais menos muito pouco tambem entao assim
    """.split()
)

#: Cobertura mínima para chamar duas ordens de "a mesma coisa". Espelha o
#: ``_REPEAT_COVERAGE`` do loop de actuation (0.7), que foi calibrado no MESMO
#: tipo de falha — modelo reembaralhando o pedido em vez de repeti-lo.
ORDER_COVERAGE = 0.7

#: Abaixo disto a ordem é curta demais para comparar por conteúdo ("tentar de
#: novo", "continuar"): só a igualdade literal significa repetição. Mesma trava
#: que faz `df -h` ser comparado só por string no loop de shell.
_MIN_ORDER_TOKENS = 3


def content_tokens(text: str) -> frozenset[str]:
    """Palavras que dizem SOBRE O QUE a ordem é (sem função gramatical)."""
    limpo = normalize_input(text)
    for sep in (",", ";", ":", "(", ")", "[", "]", "\u2014", "\u2013", "/"):
        limpo = limpo.replace(sep, " ")
    return frozenset(t for t in limpo.split() if len(t) > 2 and t not in _STOPWORDS)


def order_is_near_repeat(text: str, earlier: list[str]) -> str | None:
    """A ordem anterior que já cobre esta, ou ``None`` se ela pede coisa nova.

    ``PipelineStep.key`` só pega repetição LITERAL, e quem escreve as ordens do
    ciclo é um LLM que parafraseia. session-389114d4, interação 29 — as quatro
    ordens sintetizadas em sequência, todas pedindo a mesma pesquisa:

        1. "…com status processual para fechar a camada 5"
        2. "…com status processual real e fonte"
        3. "…real, o que foi provado, o que foi anulado ou prescrito, e a fonte"
        4. idêntica à 3          ← só AQUI a guarda antiga acordou

    Três voltas no mesmo lugar passaram batido; por conteúdo, a 2 já cobre 80%
    do que a 1 pedia. A cobertura é do texto NOVO pelo antigo, de propósito: uma
    ordem que acrescenta alvo inédito ("escrever o veredito em veredito.md") tem
    interseção baixa e passa — o mesmo princípio que faz
    ``_near_repeated_actuation`` liberar o comando quando um segmento pede algo
    que ainda não apareceu.
    """
    novos = content_tokens(text)
    if len(novos) < _MIN_ORDER_TOKENS:
        return None
    for anterior in earlier:
        antigos = content_tokens(anterior)
        if antigos and len(novos & antigos) / len(novos) >= ORDER_COVERAGE:
            return anterior
    return None


@dataclass
class Handoff:
    """A runtime request to run another pipeline next (Phase 2).

    The ``input_text`` must be a clean, imperative, self-contained instruction —
    the target pipeline runs in isolation and never sees the original message.
    """

    target_intent: IntentType
    input_text: str
    target_skill: str | None = None
    reason: str = ""
    on_failure: str = "stop"   # "stop" | "continue"
    user_visible: bool = True
    # Self-continuation: the cognitive core handing off to ITSELF to run the next
    # concrete order in the same turn, instead of ending it with a promise
    # ("sigo tentando"). Carries a bigger budget and a work note, because unlike a
    # skill handoff the next step is a *continuation* and must know what already
    # happened — otherwise it repeats the attempt that just failed.
    is_self: bool = False


@dataclass
class PipelineStep:
    """One unit of work for the orchestrator: an intent applied to an input."""

    intent: IntentType
    input_text: str
    skill_name: str | None = None
    decision: "object | None" = None   # RoutingDecision; typed loosely to avoid import cycle
    origin: str = "classifier"          # "classifier" | "handoff" | "self" | "fallback"
    depth: int = 0
    # What the previous steps of this turn already did — injected into the
    # continuation's prompt so it never redoes finished work. Built by the step
    # runner (never by the model) from the real reply/tool trace.
    context_note: str | None = None
    # Passo de AUTOCORREÇÃO do ciclo: ``"retry"`` (a ordem repetia um passo que já
    # rodou — volta ao cérebro para ele escolher outro caminho) ou ``"close"``
    # (o ciclo acabou — este passo escreve a resposta final com o que existe).
    # Serve a três coisas ao mesmo tempo: isenta o passo das guardas de repetição
    # e profundidade (senão a própria correção seria descartada), impede correção
    # de correção, e diz ao step runner qual instrução injetar.
    correction: str | None = None
    # A ordem anterior que este passo estava repetindo, para a instrução poder
    # dizer ao cérebro ONDE a resposta já está em vez de só recusar.
    covered_by: str | None = None

    def key(self) -> tuple[str, str]:
        """Stable identity for anti-loop detection (Phase 3)."""
        return (self.intent.value, normalize_input(self.input_text))

    @property
    def is_self_continuation(self) -> bool:
        """True when this step is the cognitive core continuing its own work."""
        return self.origin == "self"

    @classmethod
    def from_handoff(
        cls,
        handoff: Handoff,
        parent: "PipelineStep",
        context_note: str | None = None,
    ) -> "PipelineStep":
        """Build the next step from a handoff emitted by ``parent`` (Phase 2)."""
        return cls(
            intent=handoff.target_intent,
            input_text=handoff.input_text,
            skill_name=handoff.target_skill,
            origin="self" if handoff.is_self else "handoff",
            depth=parent.depth + 1,
            # A self-continuation inherits the accumulated note; a skill handoff
            # must stay isolated (the skill never sees this conversation).
            context_note=context_note if handoff.is_self else None,
        )

    @classmethod
    def self_correction(
        cls,
        parent: "PipelineStep",
        *,
        kind: str,
        context_note: str | None = None,
        covered_by: str | None = None,
        input_text: str | None = None,
    ) -> "PipelineStep":
        """O ciclo parou num beco: em vez de morrer, pensa de novo.

        Antes, repetição/orçamento/profundidade descartavam o passo e o turno
        terminava sem ``is_final`` — o fechamento forçado então cuspia uma string
        fixa do sistema ("Parei aqui: o próximo passo seria repetir um que já
        rodou neste turno"). Do lado do usuário isso é um erro na cara, sobre uma
        mecânica interna que ele nunca pediu.

        O passo de correção é um passo cognitivo normal: roda, escreve uma
        resposta de verdade e — sendo ``"close"``, que corre sem handoff — fecha o
        turno com fala do Neo. Mesma ideia do empurrão do loop de shell
        (``_near_repeated_actuation``): não despacha, devolve ao cérebro dizendo o
        que já está respondido e o que sobrou.

        ``depth`` é o do pai, não ``+1``: isto não é trabalho novo, é o mesmo
        trabalho sendo fechado — herdar ``+1`` faria a guarda de profundidade
        matar justamente o passo que existe para não morrer.
        """
        return cls(
            intent=parent.intent,
            input_text=input_text or parent.input_text,
            skill_name=parent.skill_name,
            origin="self",
            depth=parent.depth,
            context_note=context_note,
            correction=kind,
            covered_by=covered_by,
        )


@dataclass
class StepOutcome:
    """The result of running a single step."""

    ok: bool
    final_text: str | None = None
    handoffs: list[Handoff] = field(default_factory=list)
    # One-line record of what this step actually DID (reply + tool trace), fed to
    # the next self-continuation so the cycle builds on real results instead of
    # retrying blind. Built from execution facts, never from the model's promises.
    work_note: str | None = None
    # O step ESTACIONOU no cliente: emitiu um tool_request/command_request e o
    # trabalho de verdade só acontece quando o desktop devolver o resultado, por
    # outra entrada (`process_tool_result`). O step "terminou" do ponto de vista
    # do gerador, mas não fez nada ainda — quem tratar isso como passo concluído
    # despacha o próximo em cima de um resultado que não existe.
    parked: bool = False
    # O step EXECUTOU alguma coisa (rodou tool ou entregou arquivo)? Passo que só
    # falou não é progresso: na session-389114d4 interação 29 o ciclo deu quatro
    # voltas sem uma única tool, reescrevendo a mesma resposta, e a fila seguiu
    # porque `ok=True` ("respondeu"). O orquestrador conta os passos sem ação
    # seguidos e fecha o ciclo antes da quarta bolha idêntica.
    acted: bool = False
