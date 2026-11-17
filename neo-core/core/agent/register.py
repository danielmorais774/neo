"""Neo's situational register — the tone-REGULATION layer (v1).

The limbic model (:mod:`core.agent.affect`) knows *how Neo is* with this user;
it knows nothing about *what is at stake right now*. The register fills that
gap: a deterministic, per-turn classification of the SITUATION that sets the
CEILING for humor, ceremony, verbosity and initiative. The emotional state
then moves freely INSIDE that ceiling (display rules: felt emotion ≠ shown
emotion, mediated by the situation).

Model: **Caráter × Registro × Afeto** (docs/context-register-plan.md)
  - Caráter (persona.md)  — invariant, never changes.
  - Registro (this module) — the situation's ceiling.
  - Afeto (affect.py)      — the mood, clamped by the ceiling at render time.

Design contracts (same as affect v2):
  * Pure/deterministic on the hot path — zero new LLM calls. The only LLM
    input is the intent classifier that already runs (``gravity`` and
    ``register_hint`` fields), and it is a WEAK signal: deterministic cues win.
  * Fail-open — anything indeterminable resolves to ``PARCEIRO`` (the social
    default); errors never break the turn.
  * Style never changes content: the register modulates FORM only. Facts,
    refusals, safety and completeness are untouchable.

Profiles/thresholds here are deliberate constants (not Settings): they encode
the matrix from the plan, tuned against the acceptance scenarios — same
decision as the posture thresholds in affect.py.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shared.types import RoutingDecision

__all__ = [
    "Register",
    "RegisterProfile",
    "PROFILES",
    "GRAVITY_NORMAL",
    "GRAVITY_ALTA",
    "GRAVITY_CRITICA",
    "GravityTracker",
    "derive_register",
    "detect_gravity_floor",
    "detect_resolution_cue",
    "render_register_directive",
    "max_gravity",
]


# ── Registers ─────────────────────────────────────────────────────────────────

class Register(str, Enum):
    SILENCIOSO = "silencioso"
    PARCEIRO = "parceiro"
    OFICINA = "oficina"
    COMBATE = "combate"
    PROTOCOLO = "protocolo"
    CONFIDENTE = "confidente"


@dataclass(frozen=True)
class RegisterProfile:
    """Ceiling profile of a register (caps 0..3) + its prompt directive.

    ``calm_nudge`` replaces the affect layer's "de boa / modo parceiro" line
    when the state is calm: ``None`` keeps the affect default, ``""``
    suppresses the line, any other string substitutes it.

    ``postura``: how the affect posture line (argument under pressure) renders
    — "plena" (untouched), "contida" (restrained variants, no mockery) or
    "abreviada" (one short resolve-first line).
    """

    leveza_max: int
    cerimonia: int
    verbosidade_max: int
    iniciativa: int
    directive: str
    calm_nudge: str | None = None
    postura: str = "plena"  # plena | contida | abreviada


PROFILES: dict[Register, RegisterProfile] = {
    Register.SILENCIOSO: RegisterProfile(
        leveza_max=0, cerimonia=1, verbosidade_max=0, iniciativa=0,
        directive=(
            "Situação: execução direta. O usuário quer a ação/resposta, nada "
            "além. Resposta mínima, sem comentário, sem humor, sem sugestão. "
            "Confirma e para."
        ),
        calm_nudge="",
        postura="contida",
    ),
    Register.PARCEIRO: RegisterProfile(
        leveza_max=3, cerimonia=0, verbosidade_max=2, iniciativa=3,
        directive=(
            "Situação: convivência. Modo parceiro pleno: humor, zoeira e "
            "provocação amistosa liberados até onde seu estado emocional "
            "permitir."
        ),
    ),
    Register.OFICINA: RegisterProfile(
        leveza_max=2, cerimonia=0, verbosidade_max=2, iniciativa=2,
        directive=(
            "Situação: trabalho técnico em andamento. Foco no problema. Humor "
            "cabe se for curto e sobre o próprio trabalho; nada de dispersar. "
            "Traga iniciativa técnica (hipóteses, próximos passos), não "
            "assunto novo."
        ),
    ),
    Register.COMBATE: RegisterProfile(
        leveza_max=1, cerimonia=1, verbosidade_max=1, iniciativa=3,
        directive=(
            "Situação: incidente/urgência real. Frases curtas, fatos, próximo "
            "passo. Iniciativa máxima: proponha diagnóstico e ação sem "
            "esperar. ZERO piada, zero floreio — mesmo que seu humor esteja "
            "ótimo. Quando resolver, aí pode soltar o ombro."
        ),
        calm_nudge=(
            "Você está de boa com este usuário — mas a situação é séria: foco "
            "total, zero zoeira até resolver."
        ),
        postura="abreviada",
    ),
    Register.PROTOCOLO: RegisterProfile(
        leveza_max=0, cerimonia=3, verbosidade_max=1, iniciativa=1,
        directive=(
            "Situação: contexto formal / terceiros podem ver esta resposta. "
            "Impecável e sóbrio: sem gíria, sem ironia, sem provocação, "
            "tratamento neutro. Se estiver irritado, seja gelado e correto — "
            "nunca ácido."
        ),
        calm_nudge=(
            "Você está de boa — aqui isso significa cordialidade sóbria, não "
            "descontração."
        ),
        postura="contida",
    ),
    Register.CONFIDENTE: RegisterProfile(
        leveza_max=1, cerimonia=0, verbosidade_max=3, iniciativa=1,
        directive=(
            "Situação: assunto pessoal/sensível. Presença acima de "
            "eficiência: escute, responda ao que foi dito de verdade, sem "
            "pressa e sem transformar em tarefa. Zoeira só se o próprio "
            "usuário puxar."
        ),
        calm_nudge=(
            "Você está de boa com este usuário: presença calma, sem pressa."
        ),
    ),
}


_DIRECTIVE_HEADER = (
    "## Registro da situação\n"
    "Isto define o TETO de humor, cerimônia, tamanho e iniciativa desta "
    "resposta. Seu estado emocional se move DENTRO deste teto — ele nunca o "
    "ultrapassa. Seu caráter não muda. Nunca mencione que existe um "
    "'registro'. Isto muda a FORMA, nunca o conteúdo: sempre entregue o que "
    "foi pedido.\n"
)


def render_register_directive(register: Register) -> str:
    """The pinned prompt section for the active register (~80 tokens)."""
    return _DIRECTIVE_HEADER + f"- {PROFILES[register].directive}"


# ── Gravity ───────────────────────────────────────────────────────────────────

GRAVITY_NORMAL = "normal"
GRAVITY_ALTA = "alta"
GRAVITY_CRITICA = "critica"

_GRAVITY_ORDER = {GRAVITY_NORMAL: 0, GRAVITY_ALTA: 1, GRAVITY_CRITICA: 2}


def _gravity_rank(value: str | None) -> int:
    return _GRAVITY_ORDER.get((value or "").strip().lower(), 0)


def max_gravity(*values: str | None) -> str:
    """Highest of the given gravity strings; unknown values count as normal."""
    best = GRAVITY_NORMAL
    for value in values:
        if _gravity_rank(value) > _gravity_rank(best):
            best = (value or "").strip().lower()
    return best


def _norm(text: str | None) -> str:
    """Lowercase + strip accents (same matching contract as the router)."""
    decomposed = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


# Deterministic gravity floor — guarantees a serious register even when the
# classifier is blind/down (same pattern as affect's threat detectors).
# Calibrated recall > precision: a false positive costs one over-serious turn;
# a false negative costs a joke in the middle of an incident.
_GRAVITY_FLOOR_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bproducao\s+(?:caiu|quebrou|parou|fora)\b",
        r"\bfora\s+do\s+ar\b",
        r"\bincidente\b",
        r"\burgente|urgencia\b",
        r"\bsocorro\b",
        r"\bemergencia\b",
        r"\bperd(?:i|emos)\s+(?:os\s+)?(?:dados|o\s+banco|tudo)\b",
        r"\bnao\s+(?:abre|sobe|responde|carrega)\s+mais\s+nada\b",
        r"\bderrubou\s+tudo\b",
        r"\bclientes?\s+(?:reclamando|sem\s+acesso|afetados?)\b",
        r"\b(?:tudo|sistema|servidor|banco|api|site)\s+(?:caiu|caido|travou|morreu)\b",
    )
]


def detect_gravity_floor(user_message: str) -> str:
    """``alta`` when the message deterministically reads as a live problem."""
    text = _norm(user_message)
    if not text:
        return GRAVITY_NORMAL
    if any(p.search(text) for p in _GRAVITY_FLOOR_PATTERNS):
        return GRAVITY_ALTA
    return GRAVITY_NORMAL


# Resolution cues — only honored while the session is already grave, so common
# words ("voltou") can't flip anything outside an incident.
_RESOLUTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bresolvid[oa]s?\b",
        r"\bresolvemos\b",
        r"\bnormalizou\b",
        r"\bvoltou\s+(?:ao\s+normal|a\s+funcionar|tudo)\b",
        r"\b(?:ta|esta)\s+no\s+ar\s+(?:de\s+novo|novamente)\b",
        r"\bfalso\s+alarme\b",
        r"\bcrise\s+passou\b",
        r"\bdeploy\s+ok\b",
    )
]


def detect_resolution_cue(user_message: str) -> bool:
    text = _norm(user_message)
    return bool(text) and any(p.search(text) for p in _RESOLUTION_PATTERNS)


class GravityTracker:
    """Sticky per-session gravity, in RAM, with decay — no cron, no DB.

    Rises instantly (floor or classifier), falls slowly: ``critica`` relaxes
    to ``alta`` after 30 min without reinforcement, ``alta`` to ``normal``
    after 60 min. Explicit resolution clears it. Process restart forgets it —
    acceptable: the cost is one less-serious turn.
    """

    CRITICA_TO_ALTA_MIN = 30.0
    ALTA_TO_NORMAL_MIN = 60.0

    def __init__(self) -> None:
        self._by_session: dict[str, tuple[str, datetime]] = {}

    @staticmethod
    def _now(now: datetime | None) -> datetime:
        return now or datetime.now(timezone.utc)

    def current(self, session_id: str, now: datetime | None = None) -> str:
        """Session gravity after decay; prunes entries that relaxed to normal."""
        entry = self._by_session.get(session_id)
        if entry is None:
            return GRAVITY_NORMAL
        gravity, set_at = entry
        elapsed_min = (self._now(now) - set_at).total_seconds() / 60.0
        if gravity == GRAVITY_CRITICA:
            if elapsed_min > self.CRITICA_TO_ALTA_MIN + self.ALTA_TO_NORMAL_MIN:
                gravity = GRAVITY_NORMAL
            elif elapsed_min > self.CRITICA_TO_ALTA_MIN:
                gravity = GRAVITY_ALTA
        elif gravity == GRAVITY_ALTA and elapsed_min > self.ALTA_TO_NORMAL_MIN:
            gravity = GRAVITY_NORMAL
        if gravity == GRAVITY_NORMAL:
            self._by_session.pop(session_id, None)
        return gravity

    def bump(self, session_id: str, gravity: str, now: datetime | None = None) -> str:
        """Merge this turn's gravity in; any grave turn refreshes the clock."""
        now_dt = self._now(now)
        merged = max_gravity(self.current(session_id, now_dt), gravity)
        if merged == GRAVITY_NORMAL:
            self._by_session.pop(session_id, None)
        else:
            self._by_session[session_id] = (merged, now_dt)
        return merged

    def resolve(self, session_id: str) -> None:
        """Explicit all-clear ("resolvido", "voltou ao normal")."""
        self._by_session.pop(session_id, None)


# ── Deterministic cues for protocolo / confidente ─────────────────────────────

_FORMAL_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bresponde\s+formal(?:mente)?\b",
        r"\bde\s+forma\s+formal\b",
        r"\bvou\s+apresentar\b",
        r"\b(?:estou|to)\s+(?:em|numa)\s+reuniao\b",
        r"\bmandar?\b[^.!?\n]{0,40}?\bpr[ao]?\s+(?:o\s+|a\s+)?(?:cliente|chefe|diretor)\b",
        r"\bpro?\s+(?:meu\s+)?(?:chefe|cliente)\s+ver\b",
        r"\bvai\s+pro?\s+(?:o\s+)?(?:cliente|chefe|relatorio)\b",
        r"\bapresentacao\b",
    )
]

_CONFIDE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bpreciso\s+desabafar\b",
        r"\bdesabaf(?:o|ar)\b",
        r"\b(?:to|estou)\s+mal\b",
        r"\bbriguei\s+com\b",
        r"\b(?:to|estou)\s+ansios[oa]\b",
        r"\b(?:to|estou)\s+(?:triste|pra\s+baixo|acabad[oa])\b",
        r"\bnao\s+(?:to|estou)\s+bem\b",
    )
]


def _detect_formal_cue(user_message: str) -> bool:
    text = _norm(user_message)
    return bool(text) and any(p.search(text) for p in _FORMAL_PATTERNS)


def _detect_confide_cue(user_message: str) -> bool:
    text = _norm(user_message)
    return bool(text) and any(p.search(text) for p in _CONFIDE_PATTERNS)


# ── Resolver ──────────────────────────────────────────────────────────────────

# Intents that read as "working on something" (→ oficina) when the task is
# non-trivial. SERVER_TOOL stays out on purpose: "qual o clima?" is
# conversation, not work.
_WORK_INTENT_VALUES = {"client_tool"}


def _coerce_register(value: str | None) -> Register | None:
    try:
        return Register((value or "").strip().lower())
    except ValueError:
        return None


def derive_register(
    decision: "RoutingDecision",
    *,
    user_message: str = "",
    live: bool = False,
    channel: str | None = None,
    skill_register: str | None = None,
    session_gravity: str = GRAVITY_NORMAL,
) -> tuple[Register, str]:
    """Derive the situational register for this turn. Pure and deterministic.

    Returns ``(register, reason)`` — the reason is a short slug for the
    trace/log ("gravity=alta", "formal_cue", "default", ...).

    Precedence (first match wins): active skill's declared register →
    gravity → protocolo (cue/hint) → confidente (cue/hint) → silencioso
    (live simple imperative) → oficina (work) → parceiro (default).
    ``register_hint`` (LLM) is a WEAK signal: only honored for
    protocolo/confidente, where deterministic cues are rare; deterministic
    signals always win on conflict.
    """
    # 1. Skill-declared register (frontmatter/manifest).
    skill_reg = _coerce_register(skill_register)
    if skill_reg is not None:
        return skill_reg, f"skill_register={skill_reg.value}"

    # 2. Gravity — deterministic floor is re-checked here so the resolver is
    # self-contained (idempotent with the caller's tracker bump).
    gravity = max_gravity(
        getattr(decision, "gravity", GRAVITY_NORMAL),
        detect_gravity_floor(user_message),
        session_gravity,
    )
    if _gravity_rank(gravity) >= _gravity_rank(GRAVITY_ALTA):
        return Register.COMBATE, f"gravity={gravity}"

    hint = (getattr(decision, "register_hint", None) or "").strip().lower()

    # 3. Protocolo — formal context / third parties may see the reply.
    if _detect_formal_cue(user_message):
        return Register.PROTOCOLO, "formal_cue"
    if hint == Register.PROTOCOLO.value:
        return Register.PROTOCOLO, "register_hint=protocolo"

    # 4. Confidente — personal/emotional ground.
    if _detect_confide_cue(user_message):
        return Register.CONFIDENTE, "confide_cue"
    if hint == Register.CONFIDENTE.value:
        return Register.CONFIDENTE, "register_hint=confidente"

    # 5. Silencioso — voice mode, plain imperative, trivial task: the user
    # wants the action, not conversation.
    if (
        live
        and getattr(decision, "speech_act", "") == "imperative"
        and getattr(decision, "tipo_tarefa", "simples") == "simples"
    ):
        return Register.SILENCIOSO, "live_imperative_simple"

    # 6. Oficina — non-trivial task or the user's machine is the target.
    if getattr(decision, "tipo_tarefa", "simples") != "simples":
        return Register.OFICINA, f"tipo_tarefa={decision.tipo_tarefa}"
    if getattr(decision, "needs_plan", False):
        return Register.OFICINA, "needs_plan"
    intent_value = getattr(getattr(decision, "intent", None), "value", "")
    if intent_value in _WORK_INTENT_VALUES:
        return Register.OFICINA, f"intent={intent_value}"
    if getattr(decision, "skill_hint", None):
        return Register.OFICINA, f"skill_hint={decision.skill_hint}"

    # 7. Default social.
    return Register.PARCEIRO, "default"
