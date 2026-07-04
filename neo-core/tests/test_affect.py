"""Tests for Neo's per-user emotional state (cortisol) — pure logic + injection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core.agent.affect import (
    AffectBand,
    AffectSignal,
    AffectState,
    apply_decay,
    band,
    detect_wrong_name,
    merge_signal,
    render_directive,
)
from core.agent.affect import AffectAppraiser
from core.agent.context_selector import PromptContextSelector
from shared.config import Settings
from shared.types import ContextLayer, IntentType, RoutingDecision


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── AffectState (de)serialization ─────────────────────────────────────────────

def test_from_field_handles_none_dict_and_junk():
    assert AffectState.from_field(None).cortisol == 0.0
    assert AffectState.from_field("not json").cortisol == 0.0
    assert AffectState.from_field(123).cortisol == 0.0
    state = AffectState.from_field({"cortisol": 0.7, "last_trigger": "wrong_name"})
    assert state.cortisol == 0.7
    assert state.last_trigger == "wrong_name"
    # JSON string round-trips too
    assert AffectState.from_field('{"cortisol": 0.5}').cortisol == 0.5


def test_to_field_roundtrip():
    state = AffectState(cortisol=0.3, valence=-0.2, last_trigger="insult")
    assert AffectState.from_field(state.to_field()).cortisol == 0.3


# ── Decay ─────────────────────────────────────────────────────────────────────

def test_decay_halves_after_one_half_life():
    now = _now()
    state = AffectState(cortisol=0.8, valence=0.4, updated_at=(now - timedelta(minutes=45)).isoformat())
    decayed = apply_decay(state, half_life_min=45.0, now=now)
    assert decayed.cortisol == pytest.approx(0.4, abs=1e-6)
    assert decayed.valence == pytest.approx(0.2, abs=1e-6)


def test_decay_noop_without_timestamp():
    state = AffectState(cortisol=0.8)
    assert apply_decay(state, half_life_min=45.0).cortisol == 0.8


def test_decay_two_half_lives_quarters():
    now = _now()
    state = AffectState(cortisol=1.0, updated_at=(now - timedelta(minutes=90)).isoformat())
    assert apply_decay(state, half_life_min=45.0, now=now).cortisol == pytest.approx(0.25, abs=1e-6)


# ── Bands ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "cortisol,expected",
    [
        (0.0, AffectBand.CALM),
        (0.24, AffectBand.CALM),
        (0.25, AffectBand.IRRITATED),
        (0.59, AffectBand.IRRITATED),
        (0.6, AffectBand.PISSED),
        (1.0, AffectBand.PISSED),
    ],
)
def test_band_thresholds(cortisol, expected):
    assert band(cortisol, irritated=0.25, pissed=0.6) is expected


# ── Merge / clamp ─────────────────────────────────────────────────────────────

def test_merge_clamps_positive_delta():
    state = AffectState(cortisol=0.5)
    merged = merge_signal(
        state,
        AffectSignal(trigger="insult", cortisol_delta=0.9, why="xingou"),
        max_up=0.4, max_down=0.3, recent_max=8,
    )
    # delta clamped to +0.4 → 0.9
    assert merged.cortisol == pytest.approx(0.9, abs=1e-6)
    assert merged.last_trigger == "insult"
    assert merged.updated_at is not None
    assert merged.recent[-1]["why"] == "xingou"


def test_merge_clamps_negative_delta_and_floor_zero():
    state = AffectState(cortisol=0.1)
    merged = merge_signal(
        state,
        AffectSignal(trigger="compliment", cortisol_delta=-0.9),
        max_up=0.4, max_down=0.3, recent_max=8,
    )
    # -0.9 clamped to -0.3 → 0.1-0.3 = -0.2 → floored to 0.0
    assert merged.cortisol == 0.0


def test_merge_ring_buffer_bounded():
    state = AffectState(cortisol=0.0)
    for i in range(10):
        state = merge_signal(
            state, AffectSignal(trigger="teasing", cortisol_delta=0.05, why=f"e{i}"),
            max_up=0.4, max_down=0.3, recent_max=3,
        )
    assert len(state.recent) == 3
    assert state.recent[-1]["why"] == "e9"


# ── Directive rendering ───────────────────────────────────────────────────────

def test_directive_calm_is_light():
    text = render_directive(AffectState(cortisol=0.05), irritated=0.25, pissed=0.6)
    assert text is not None
    assert "de boa" in text.lower()


def test_directive_pissed_is_harsh_and_still_delivers():
    state = AffectState(cortisol=0.8, recent=[{"why": "me chamou de Alexa"}])
    text = render_directive(state, irritated=0.25, pissed=0.6)
    assert "puto" in text.lower()
    assert "resolve" in text.lower()  # never turns harshness into refusal
    assert "me chamou de Alexa" in text  # surfaces the recent reason


# ── Deterministic wrong-name detector ─────────────────────────────────────────

def test_detect_wrong_name():
    assert detect_wrong_name("Alexa, liga a luz da sala") is True
    assert detect_wrong_name("chatgpt me explica isso") is True
    assert detect_wrong_name("que horas são?") is False
    assert detect_wrong_name("") is False


# ── Appraiser JSON parsing ────────────────────────────────────────────────────

def test_appraiser_parse_extracts_json():
    raw = 'blah {"trigger":"insult","cortisol_delta":0.2,"valence_delta":-0.1,"why":"grosso"} tail'
    sig = AffectAppraiser._parse(raw)
    assert sig.trigger == "insult"
    assert sig.cortisol_delta == 0.2
    assert sig.why == "grosso"


def test_appraiser_parse_junk_returns_neutral():
    sig = AffectAppraiser._parse("no json here")
    assert sig.trigger == "neutral"
    assert sig.cortisol_delta == 0.0


@pytest.mark.asyncio
async def test_appraiser_appraise_parses_provider_response():
    provider = AsyncMock()
    completion = MagicMock()
    completion.get_content = MagicMock(
        return_value='{"trigger":"wrong_name","cortisol_delta":0.3,"valence_delta":0.0,"why":"chamou de Siri"}'
    )
    provider.complete = AsyncMock(return_value=completion)

    llm = MagicMock()
    llm.list_available_providers = MagicMock(return_value=["deepseek"])
    llm.get_provider = MagicMock(return_value=provider)

    settings = MagicMock(spec=Settings)
    settings.neo_affect_model = ""

    appraiser = AffectAppraiser(llm, settings, db=None)
    sig = await appraiser.appraise(user_message="Siri, que horas são", assistant_response="14h", current=AffectState())
    assert sig.trigger == "wrong_name"
    assert sig.cortisol_delta == 0.3


# ── Injection into the built prompt ───────────────────────────────────────────

def _selector_with_affect(affect: AffectState) -> PromptContextSelector:
    token_counter = MagicMock()
    token_counter.count = lambda text: len(text.split()) * 2

    settings = MagicMock(spec=Settings)
    settings.context_budget_neo_core_ctx = 600
    settings.context_budget_user_prefs = 500
    settings.context_budget_user_pks = 900
    settings.context_budget_retrieved = 1200
    settings.context_relevance_threshold = 0.25
    settings.neo_affect_enabled = True
    settings.neo_affect_half_life_min = 45.0
    settings.neo_affect_band_irritated = 0.25
    settings.neo_affect_band_pissed = 0.6

    pks = AsyncMock()
    pks.retrieve_system_context = AsyncMock(return_value="")
    pks.retrieve_context = AsyncMock(return_value=({"candidates": []}, ""))
    pks.get_user_affect = AsyncMock(return_value=affect)

    return PromptContextSelector(token_counter=token_counter, settings=settings, pks=pks)


def _decision() -> RoutingDecision:
    return RoutingDecision(
        intent=IntentType.DIRECT_RESPONSE, confidence=0.9, needs_memory=False, knowledge_keywords=None,
    )


@pytest.mark.asyncio
async def test_affect_injected_when_enabled_and_pissed():
    # fresh timestamp so decay doesn't erase the level
    affect = AffectState(cortisol=0.8, updated_at=_now().isoformat(), last_trigger="wrong_name")
    sel = _selector_with_affect(affect)

    system_prompt, trace = await sel.build_prompt(
        user_message="oi", user_id=uuid4(), decision=_decision(),
        system_context=None, recovered_context=[], is_neo_training=False,
    )

    affect_candidates = [c for c in trace if c.layer == ContextLayer.NEO_AFFECT]
    assert affect_candidates and affect_candidates[0].included
    assert affect_candidates[0].pin is True
    assert "puto" in system_prompt.lower()


@pytest.mark.asyncio
async def test_affect_absent_when_disabled():
    affect = AffectState(cortisol=0.8, updated_at=_now().isoformat())
    sel = _selector_with_affect(affect)
    sel._settings.neo_affect_enabled = False

    _, trace = await sel.build_prompt(
        user_message="oi", user_id=uuid4(), decision=_decision(),
        system_context=None, recovered_context=[], is_neo_training=False,
    )
    assert not [c for c in trace if c.layer == ContextLayer.NEO_AFFECT]
