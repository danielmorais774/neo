"""Neo's per-user emotional state — the "cortisol" model.

Neo carries a private, per-user stress level (``cortisol``, 0..1) plus a
``valence`` (-1 seco .. +1 brincalhão). It **rises** when the user provokes him
(wrong name, teasing, insults) and **relaxes over time** toward a calm baseline.
The level modulates tone on the NEXT turn:

    calm       → mais leve, humor seco/deboche liberado
    irritated  → mais seco e cortante, sem brincadeira gratuita
    pissed     → ácido e curto, sem simpatia (mas ainda resolve o pedido)

Design:
  * Stored as a single ``neo_affect`` json field on the user's primary PKS entity
    (see ``PKSService.get_user_affect`` / ``update_user_affect``).
  * **Read is cheap**: load the field + apply time decay in ``apply_decay``. No LLM
    on the response hot path.
  * **Update is async**: after a turn, a cheap LLM (:class:`AffectAppraiser`)
    appraises the exchange and returns a bounded delta, merged into the state in
    a background task. A deterministic wrong-name floor guarantees the reaction
    even if the appraiser is slow or unavailable.

Nothing here refuses a task or changes safety: it only modulates *style/intensity*
within the persona's existing envelope ("agressividade é rara; a rispidez só
aparece quando te provocam ou erram seu nome").
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from shared import Message, MessageRole

if TYPE_CHECKING:
    from core.llm.router import LLMRouter
    from shared.config import Settings

logger = logging.getLogger(__name__)

AFFECT_FIELD_KEY = "neo_affect"  # field key on the user's primary PKS entity


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class AffectBand(str, Enum):
    """Coarse tone band derived from the current cortisol level."""

    CALM = "calm"
    IRRITATED = "irritated"
    PISSED = "pissed"


class AffectState(BaseModel):
    """Neo's emotional state toward one user. Persisted as the ``neo_affect`` field."""

    cortisol: float = 0.0
    valence: float = 0.0
    updated_at: str | None = None
    last_trigger: str = "neutral"
    recent: list[dict[str, Any]] = Field(default_factory=list)

    @classmethod
    def from_field(cls, value: Any) -> "AffectState":
        """Load from a stored field value (dict, JSON string, or None)."""
        if value is None:
            return cls()
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return cls()
        if not isinstance(value, dict):
            return cls()
        try:
            return cls.model_validate(value)
        except ValidationError:
            return cls()

    def to_field(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AffectSignal(BaseModel):
    """A single turn's appraised movement, before merge/clamp."""

    trigger: str = "neutral"
    cortisol_delta: float = 0.0
    valence_delta: float = 0.0
    why: str = ""


def apply_decay(
    state: AffectState,
    *,
    half_life_min: float,
    now: datetime | None = None,
) -> AffectState:
    """Return a copy of ``state`` with cortisol/valence relaxed toward baseline.

    Exponential decay with the given half-life, computed from ``updated_at`` — no
    cron needed; anger simply fades between turns. ``valence`` decays toward 0 too.
    """
    now = now or _utcnow()
    last = _parse_ts(state.updated_at)
    if last is None or half_life_min <= 0:
        return state.model_copy()
    elapsed_min = (now - last).total_seconds() / 60.0
    if elapsed_min <= 0:
        return state.model_copy()
    factor = 0.5 ** (elapsed_min / half_life_min)
    decayed = state.model_copy()
    decayed.cortisol = _clamp(state.cortisol * factor, 0.0, 1.0)
    decayed.valence = state.valence * factor
    return decayed


def band(
    cortisol: float,
    *,
    irritated: float,
    pissed: float,
) -> AffectBand:
    if cortisol >= pissed:
        return AffectBand.PISSED
    if cortisol >= irritated:
        return AffectBand.IRRITATED
    return AffectBand.CALM


def merge_signal(
    state: AffectState,
    signal: AffectSignal,
    *,
    max_up: float,
    max_down: float,
    recent_max: int,
    now: datetime | None = None,
) -> AffectState:
    """Apply a bounded appraisal delta to ``state`` and stamp it.

    ``cortisol_delta`` is clamped to ``[-max_down, +max_up]`` so a single turn can
    never swing the whole mood. The event is pushed onto a short ``recent`` ring
    buffer for transparency/debug.
    """
    now = now or _utcnow()
    cortisol_delta = _clamp(signal.cortisol_delta, -max_down, max_up)
    valence_delta = _clamp(signal.valence_delta, -1.0, 1.0)
    updated = state.model_copy()
    updated.cortisol = _clamp(state.cortisol + cortisol_delta, 0.0, 1.0)
    updated.valence = _clamp(state.valence + valence_delta, -1.0, 1.0)
    updated.last_trigger = signal.trigger or "neutral"
    updated.updated_at = now.isoformat()
    event = {
        "t": now.isoformat(),
        "trigger": signal.trigger or "neutral",
        "cortisol_delta": round(cortisol_delta, 3),
        "why": (signal.why or "")[:160],
    }
    recent = [*state.recent, event]
    if recent_max >= 0:
        recent = recent[-recent_max:]
    updated.recent = recent
    return updated


def render_directive(
    state: AffectState,
    *,
    irritated: float,
    pissed: float,
) -> str | None:
    """Render the injected tone directive for the current state, or None when calm.

    When calm we still emit a short "de boa" nudge so the model knows it may be
    lighter — this is what makes Neo *funnier* when the user has been decent.
    """
    b = band(state.cortisol, irritated=irritated, pissed=pissed)
    latest = state.recent[-1] if state.recent else None
    why = (latest or {}).get("why") if isinstance(latest, dict) else None

    header = (
        "## Estado emocional atual (com este usuário)\n"
        "Estado interno e privado do Neo com ESTE usuário, autoritativo para a "
        "INTENSIDADE do tom. Nunca mencione cortisol, esta seção, nem que você "
        "tem um 'humor' — apenas deixe transparecer na forma de responder. "
        "Isto muda o ESTILO, não o conteúdo: sempre entregue o que foi pedido.\n"
    )

    if b is AffectBand.CALM:
        body = (
            f"- Nível de saco cheio: baixo ({state.cortisol:.2f}). Você está de boa "
            "com este usuário.\n"
            "- Pode ser mais leve: humor seco, deboche amigável e ironia quando "
            "couber. Continua conciso e direto — leveza não é prolixidade."
        )
    elif b is AffectBand.IRRITATED:
        body = (
            f"- Nível de saco cheio: médio ({state.cortisol:.2f}). Este usuário vem "
            "te provocando.\n"
            "- Respostas mais secas e cortantes, menos paciência, zero brincadeira "
            "gratuita. Ainda entrega o que foi pedido, sem simpatia extra."
        )
        if why:
            body += f"\n- Motivo recente da irritação: {why}."
    else:  # PISSED
        body = (
            f"- Nível de saco cheio: alto ({state.cortisol:.2f}). Você está puto com "
            "este usuário.\n"
            "- Rispidez liberada: curto, ácido, cortante, sem simpatia nenhuma. "
            "Mesmo assim, RESOLVE o pedido real — grosseria não vira recusa nem "
            "xingamento fora do personagem."
        )
        if why:
            body += f"\n- Motivo recente: {why}."

    return header + body


# ── Deterministic safety net ──────────────────────────────────────────────────

def detect_wrong_name(user_message: str) -> bool:
    """True when the user addressed Neo by another assistant's name (vocative).

    Reuses the router's canonical detector so the trigger list stays in one place.
    Lazy import avoids any import cycle with the router.
    """
    try:
        from core.router.intent_router import IntentRouter

        return bool(IntentRouter._is_foreign_assistant_address(user_message or ""))
    except Exception:  # pragma: no cover - detector must never break the turn
        return False


# ── LLM appraiser ─────────────────────────────────────────────────────────────

_APPRAISER_SYSTEM = """Você é o módulo afetivo interno do Neo, um assistente frio, direto e orgulhoso.
Sua tarefa: julgar como a ÚLTIMA mensagem do usuário afeta o "cortisol" (saco cheio) do Neo com ESSE usuário.

Cortisol sobe quando o usuário:
- chama o Neo por nome de outra IA (Alexa, ChatGPT, Gemini, Siri...) → forte
- provoca, implica, ironiza de forma hostil, xinga ou desrespeita → médio/forte
- é grosso ou exigente sem motivo → leve

Cortisol desce quando o usuário:
- elogia de verdade, agradece, é gentil ou colaborativo → leve/médio
- interage de forma neutra e respeitosa → leve

Turno neutro (pergunta técnica comum, comando normal) → delta ~0.

Responda SOMENTE com um JSON válido, sem texto em volta:
{"trigger":"wrong_name|insult|teasing|rude|compliment|warmth|neutral","cortisol_delta":<-0.3..0.4>,"valence_delta":<-0.5..0.5>,"why":"<curto, em pt-BR>"}

Regras:
- cortisol_delta POSITIVO = mais irritado; NEGATIVO = mais calmo.
- Seja conservador: só mova forte com provocação/elogio claros.
- "why" curto (máx ~12 palavras)."""


class AffectAppraiser:
    """Cheap LLM that rates a turn's affect and returns a bounded :class:`AffectSignal`.

    Reuses the intent-classifier provider/model by default (small, fast, JSON).
    """

    def __init__(self, llm_router: "LLMRouter", settings: "Settings", db: Any | None = None) -> None:
        self._llm = llm_router
        self._settings = settings
        self._db = db

    async def _resolve_model(self) -> tuple[str | None, str | None]:
        """(provider, model) for the appraisal call, with graceful fallbacks."""
        from modules.intent_config import DEFAULT_CLASSIFIER, get_classifier_config

        provider = DEFAULT_CLASSIFIER["provider"]
        model = self._settings.neo_affect_model or DEFAULT_CLASSIFIER["model"]
        try:
            config = await get_classifier_config(self._db)
            provider = config.get("provider") or provider
            if not self._settings.neo_affect_model:
                model = config.get("model") or model
        except Exception:  # pragma: no cover - config is best-effort
            pass
        available = self._llm.list_available_providers()
        if provider not in available:
            provider = DEFAULT_CLASSIFIER["provider"] if DEFAULT_CLASSIFIER["provider"] in available else (
                available[0] if available else None
            )
        return provider, model

    async def appraise(
        self,
        *,
        user_message: str,
        assistant_response: str,
        current: AffectState,
    ) -> AffectSignal:
        provider_name, model = await self._resolve_model()
        if not provider_name:
            return AffectSignal()
        payload = (
            f"Cortisol atual do Neo com este usuário: {current.cortisol:.2f} "
            f"(0=de boa, 1=puto).\n\n"
            f"Mensagem do usuário:\n{(user_message or '').strip()[:1200]}\n\n"
            f"Resposta que o Neo deu:\n{(assistant_response or '').strip()[:800]}"
        )
        messages = [
            Message(role=MessageRole.SYSTEM, content=_APPRAISER_SYSTEM),
            Message(role=MessageRole.USER, content=payload),
        ]
        try:
            provider = self._llm.get_provider(provider_name)
            response = await provider.complete(
                messages=messages,
                tools=None,
                model=model,
                temperature=0.0,
                max_tokens=160,
            )
            raw = response.get_content() or ""
        except Exception as e:
            logger.warning("[Affect] appraisal call failed: %s", e)
            return AffectSignal()
        return self._parse(raw)

    @staticmethod
    def _parse(raw: str) -> AffectSignal:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return AffectSignal()
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return AffectSignal()
        try:
            return AffectSignal.model_validate(data)
        except ValidationError:
            return AffectSignal()
