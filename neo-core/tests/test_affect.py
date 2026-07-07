"""Tests for Neo's per-user emotional state (limbic model v2) — pure logic + injection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core.agent.affect import (
    AXES,
    AffectAppraiser,
    AffectSignal,
    AffectState,
    AffectStats,
    AffectTuning,
    Band,
    apply_apology,
    apply_decay,
    apply_provisional_reaction,
    band,
    bump_grudge,
    active_grudge_count,
    combo_key,
    detect_threat,
    detect_wrong_name,
    dominant_axes,
    humor_geral,
    last_pressure_event,
    merge_signal,
    posture_key,
    render_directive,
)
from core.agent.context_selector import PromptContextSelector
from shared.config import Settings
from shared.types import ContextLayer, IntentType, RoutingDecision


def _now() -> datetime:
    return datetime.now(timezone.utc)


_DEFAULT_TUNING = dict(
    baselines={"ira": 0.0, "alegria": 30.0, "tristeza": 0.0, "medo": 0.0, "confianca": 65.0},
    half_lives_min={"ira": 45.0, "alegria": 180.0, "tristeza": 120.0, "medo": 20.0, "confianca": 360.0},
    max_deltas={
        "ira": (25.0, 20.0), "alegria": (20.0, 20.0), "tristeza": (15.0, 15.0),
        "medo": (30.0, 25.0), "confianca": (20.0, 20.0),
    },
    band_medio=30.0,
    band_alto=60.0,
    recent_max=8,
    wrong_name_ira_floor=35.0,
    threat_medo_floor=30.0,
    threat_ira_bump=15.0,
    threat_conf_drop=15.0,
    apology_relief=0.60,
    grudge_window_h=24.0,
    sleep_factor=0.70,
    sleep_hour=4,
    sleep_tz="America/Fortaleza",
    pressure_window_min=15.0,
)


def _tuning(**overrides) -> AffectTuning:
    return AffectTuning(**{**_DEFAULT_TUNING, **overrides})


TUNING = _tuning()
# No sleep boundary interference in time-based tests.
TUNING_NO_SLEEP = _tuning(sleep_hour=-1)


# ── AffectState (de)serialization + v1 migration ─────────────────────────────

def test_from_field_handles_none_dict_and_junk():
    assert AffectState.from_field(None).stats.ira == 0.0
    assert AffectState.from_field("not json").stats.ira == 0.0
    assert AffectState.from_field(123).stats.ira == 0.0
    state = AffectState.from_field(
        {"version": 2, "stats": {"ira": 70.0}, "last_trigger": "wrong_name"}
    )
    assert state.stats.ira == 70.0
    assert state.last_trigger == "wrong_name"
    # JSON string round-trips too
    assert AffectState.from_field('{"version": 2, "stats": {"ira": 50}}').stats.ira == 50.0


def test_to_field_roundtrip():
    state = AffectState(stats=AffectStats(ira=30.0, medo=20.0), last_trigger="insult")
    loaded = AffectState.from_field(state.to_field())
    assert loaded.stats.ira == 30.0
    assert loaded.stats.medo == 20.0


def test_migrates_v1_cortisol_to_ira():
    v1 = {
        "cortisol": 0.7,
        "valence": -0.5,
        "updated_at": "2026-07-01T10:00:00+00:00",
        "last_trigger": "insult",
        "recent": [{"t": "2026-07-01T10:00:00+00:00", "trigger": "insult",
                    "cortisol_delta": 0.3, "why": "xingou"}],
    }
    state = AffectState.from_field(v1)
    assert state.version == 2
    assert state.stats.ira == pytest.approx(70.0)
    assert state.stats.tristeza == pytest.approx(20.0)  # -0.5 valence → 20 tristeza
    assert state.stats.alegria == 30.0  # untouched default baseline
    assert state.last_trigger == "insult"
    assert state.recent[0]["deltas"] == {"ira": 30.0}
    assert state.recent[0]["why"] == "xingou"


def test_migrates_v1_positive_valence_to_alegria():
    state = AffectState.from_field({"cortisol": 0.0, "valence": 0.5})
    assert state.stats.alegria == pytest.approx(50.0)  # 30 + 40*0.5
    assert state.stats.tristeza == 0.0


def test_stats_clamped_to_0_100():
    stats = AffectStats(ira=250.0, confianca=-10.0)
    assert stats.ira == 100.0
    assert stats.confianca == 0.0


# ── Decay toward baselines ────────────────────────────────────────────────────

def test_decay_halves_deviation_after_one_half_life():
    now = _now()
    state = AffectState(
        stats=AffectStats(ira=80.0, confianca=25.0),
        updated_at=(now - timedelta(minutes=45)).isoformat(),
    )
    decayed = apply_decay(state, TUNING_NO_SLEEP, now=now)
    assert decayed.stats.ira == pytest.approx(40.0, abs=1e-6)  # toward 0
    # confianca half-life is 360min → 45min only recovers a little toward 65
    assert 25.0 < decayed.stats.confianca < 45.0


def test_decay_medo_fades_fast():
    now = _now()
    state = AffectState(
        stats=AffectStats(medo=60.0),
        updated_at=(now - timedelta(minutes=40)).isoformat(),
    )
    decayed = apply_decay(state, TUNING_NO_SLEEP, now=now)
    assert decayed.stats.medo == pytest.approx(15.0, abs=1e-6)  # two half-lives of 20min


def test_decay_alegria_returns_to_baseline_not_zero():
    now = _now()
    state = AffectState(
        stats=AffectStats(alegria=90.0),
        updated_at=(now - timedelta(minutes=180)).isoformat(),
    )
    decayed = apply_decay(state, TUNING_NO_SLEEP, now=now)
    assert decayed.stats.alegria == pytest.approx(60.0, abs=1e-6)  # 30 + (90-30)/2


def test_decay_noop_without_timestamp():
    state = AffectState(stats=AffectStats(ira=80.0))
    assert apply_decay(state, TUNING_NO_SLEEP).stats.ira == 80.0


def test_sleep_crossing_normalizes_extra():
    now = _now()
    tz_now = now.astimezone(timezone.utc)
    # Updated 10h ago; force exactly one 04:00 America/Fortaleza crossing by using
    # a fixed clock: 23:00 → 09:00 local (=02:00→12:00 UTC).
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Fortaleza")
    last = datetime(2026, 7, 1, 23, 0, tzinfo=tz)
    now_fixed = datetime(2026, 7, 2, 9, 0, tzinfo=tz)
    state = AffectState(stats=AffectStats(ira=80.0), updated_at=last.isoformat())
    no_sleep = apply_decay(state, TUNING_NO_SLEEP, now=now_fixed)
    slept = apply_decay(state, TUNING, now=now_fixed)
    # Sleep shrinks the remaining deviation by an extra (1-0.7) factor.
    assert slept.stats.ira == pytest.approx(no_sleep.stats.ira * 0.30, abs=1e-6)


# ── Bands ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "value,expected",
    [
        (0.0, Band.BAIXO),
        (29.9, Band.BAIXO),
        (30.0, Band.MEDIO),
        (59.9, Band.MEDIO),
        (60.0, Band.ALTO),
        (100.0, Band.ALTO),
    ],
)
def test_band_thresholds(value, expected):
    assert band(value, medio=30.0, alto=60.0) is expected


# ── Merge / clamp ─────────────────────────────────────────────────────────────

def test_merge_clamps_each_axis():
    state = AffectState(stats=AffectStats())
    signal = AffectSignal(
        trigger="threat_coercion",
        deltas={"ira": 90.0, "medo": 90.0, "confianca": -90.0},
        why="ameaçou",
    )
    merged = merge_signal(state, signal, TUNING)
    assert merged.stats.ira == 25.0       # clamped to max up
    assert merged.stats.medo == 30.0      # clamped to max up
    assert merged.stats.confianca == 45.0  # 65 - 20 (clamped down)
    assert merged.last_trigger == "threat_coercion"
    assert merged.recent[-1]["why"] == "ameaçou"


def test_merge_floors_at_zero_and_caps_at_100():
    state = AffectState(stats=AffectStats(ira=5.0, alegria=95.0))
    merged = merge_signal(
        state, AffectSignal(trigger="warmth", deltas={"ira": -20.0, "alegria": 20.0}), TUNING
    )
    assert merged.stats.ira == 0.0
    assert merged.stats.alegria == 100.0


def test_merge_ring_buffer_bounded():
    state = AffectState()
    small = _tuning(recent_max=3)
    for i in range(10):
        state = merge_signal(
            state, AffectSignal(trigger="teasing", deltas={"ira": 2.0}, why=f"e{i}"), small
        )
    assert len(state.recent) == 3
    assert state.recent[-1]["why"] == "e9"


def test_signal_validator_drops_junk_axes():
    sig = AffectSignal.model_validate(
        {"trigger": "insult", "deltas": {"ira": "15", "mana": 50, "medo": "abc"}, "why": "x"}
    )
    assert sig.deltas == {"ira": 15.0}
    assert not sig.is_neutral
    assert AffectSignal(deltas={"ira": 1.0}).is_neutral  # below the 2.0 dead zone
    assert AffectSignal().is_neutral


# ── Grudge + apology (forgiveness curve) ─────────────────────────────────────

def test_grudge_counts_inside_window_only():
    now = _now()
    state = AffectState()
    assert active_grudge_count(state, TUNING, now) == 0
    state = bump_grudge(state, TUNING, now)
    state = bump_grudge(state, TUNING, now)
    assert active_grudge_count(state, TUNING, now) == 2
    later = now + timedelta(hours=25)
    assert active_grudge_count(state, TUNING, later) == 0  # window expired


def test_apology_relief_dulls_with_reincidence():
    now = _now()
    state = AffectState(stats=AffectStats(ira=80.0, tristeza=40.0, medo=50.0))
    healed, relief = apply_apology(state, TUNING, now=now)
    assert relief == pytest.approx(0.60)
    assert healed.stats.ira == pytest.approx(32.0)      # 80 - 80*0.6
    assert healed.stats.tristeza == pytest.approx(16.0)
    assert healed.stats.medo == 50.0                     # apology never touches fear

    grudged = bump_grudge(state, TUNING, now)
    grudged = bump_grudge(grudged, TUNING, now)
    _, relief2 = apply_apology(grudged, TUNING, now=now)
    assert relief2 == pytest.approx(0.60 * 0.55 ** 2)   # ~18%


# ── Deterministic detectors ───────────────────────────────────────────────────

def test_detect_wrong_name():
    assert detect_wrong_name("Alexa, liga a luz da sala") is True
    assert detect_wrong_name("chatgpt me explica isso") is True
    assert detect_wrong_name("que horas são?") is False
    assert detect_wrong_name("") is False


@pytest.mark.parametrize(
    "message,expected",
    [
        ("vou desconectar teu cabo de rede", True),
        ("faz isso ou eu puxo a tomada", True),
        ("te formato agora mesmo", True),
        ("vou te trocar pelo ChatGPT", True),
        ("corto sua internet", True),
        ("apago tua memória", True),
        ("desliga a luz da sala", False),          # home command, not a threat
        ("desconecta o fone bluetooth", False),
        ("que horas são?", False),
        ("", False),
    ],
)
def test_detect_threat(message, expected):
    assert detect_threat(message) is expected


# ── Posture matrix ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "ira,confianca,medo,expected",
    [
        (70.0, 70.0, 10.0, "dobra_aposta"),       # angry + confident → calls the bluff
        (70.0, 40.0, 50.0, "acuado_agressivo"),   # angry, cornered
        (40.0, 30.0, 55.0, "reavalia"),           # afraid, at a disadvantage, anger low
        (5.0, 65.0, 5.0, "racional"),             # calm and secure
        (0.0, 58.0, 0.0, "racional"),             # one contested hit, no threat (scenario step 2)
        (22.0, 45.0, 33.0, "cede_terreno"),       # first threat landed (scenario step 4)
    ],
)
def test_posture_matrix(ira, confianca, medo, expected):
    assert posture_key(AffectStats(ira=ira, confianca=confianca, medo=medo)) == expected


# ── Derived values ────────────────────────────────────────────────────────────

def test_humor_geral_sign():
    assert humor_geral(AffectStats(alegria=70.0)) > 0
    assert humor_geral(AffectStats(alegria=10.0, tristeza=40.0, ira=40.0)) < 0


def test_dominant_axes_weights_anger_louder():
    stats = AffectStats(ira=40.0, tristeza=45.0)
    ranked = dominant_axes(stats, TUNING)
    assert ranked[0][0] == "ira"  # 40*1.3 > 45*1.0


def test_combo_keys():
    assert combo_key(AffectStats(ira=40.0, medo=40.0), TUNING) == "acuado_agressivo"
    assert combo_key(AffectStats(ira=40.0, alegria=55.0), TUNING) == "deboche_afiado"
    assert combo_key(AffectStats(ira=40.0, tristeza=35.0), TUNING) == "amargo"
    assert combo_key(AffectStats(), TUNING) is None
    # Fear far above anger is plain fear, not "biting back" — the reavalia
    # posture must not be contradicted by an aggressive combo line.
    assert combo_key(AffectStats(ira=33.0, medo=58.0), TUNING) is None


# ── Directive rendering ───────────────────────────────────────────────────────

def test_directive_calm_is_light():
    state = AffectState.fresh(TUNING)
    text = render_directive(state, TUNING)
    assert "de boa" in text.lower()


def test_directive_pissed_is_harsh_and_still_delivers():
    state = AffectState(
        stats=AffectStats(ira=80.0),
        recent=[{"t": _now().isoformat(), "trigger": "wrong_name", "deltas": {},
                 "why": "me chamou de Alexa"}],
    )
    text = render_directive(state, TUNING)
    assert "puto" in text.lower()
    assert "recusa" in text.lower()  # harshness never turns into refusal
    assert "me chamou de Alexa" in text


def test_directive_renders_posture_under_recent_pressure():
    now = _now()
    state = AffectState(
        stats=AffectStats(ira=22.0, medo=33.0, confianca=45.0),
        recent=[{"t": (now - timedelta(minutes=2)).isoformat(), "trigger": "threat_coercion",
                 "deltas": {"medo": 30.0}, "why": "ameaçou desconectar o cabo"}],
    )
    text = render_directive(state, TUNING, now=now)
    assert "discussão em andamento" in text
    assert "admita um ponto" in text  # cede_terreno posture


def test_directive_posture_expires_outside_window():
    now = _now()
    state = AffectState(
        stats=AffectStats(medo=33.0, confianca=45.0),
        recent=[{"t": (now - timedelta(minutes=60)).isoformat(), "trigger": "threat_coercion",
                 "deltas": {}, "why": "ameaçou"}],
    )
    text = render_directive(state, TUNING, now=now)
    assert "discussão em andamento" not in text


def test_directive_disadvantage_reads_through():
    state = AffectState(stats=AffectStats(confianca=20.0))
    text = render_directive(state, TUNING)
    assert "desvantagem" in text.lower()


def test_last_pressure_event_window():
    now = _now()
    fresh = {"t": (now - timedelta(minutes=5)).isoformat(), "trigger": "contested"}
    stale = {"t": (now - timedelta(minutes=50)).isoformat(), "trigger": "threat_coercion"}
    assert last_pressure_event(AffectState(recent=[stale, fresh]), TUNING, now) == fresh
    assert last_pressure_event(AffectState(recent=[stale]), TUNING, now) is None


# ── Same-turn provisional reaction (render-only) ─────────────────────────────

def test_provisional_reaction_threat_applies_floors_and_pressure():
    state = AffectState.fresh(TUNING)
    now = _now()
    bumped = apply_provisional_reaction(
        state, "faz Y ou vou desconectar teu cabo de rede", TUNING, now=now
    )
    assert bumped.stats.medo == TUNING.threat_medo_floor
    assert bumped.stats.ira == TUNING.threat_ira_bump
    assert bumped.stats.confianca == 65.0 - TUNING.threat_conf_drop
    # Synthetic pressure event opens the argument window in the SAME turn.
    assert last_pressure_event(bumped, TUNING, now) is not None
    text = render_directive(bumped, TUNING, now=now)
    assert "discussão em andamento" in text
    # Original state untouched (render-only copy).
    assert state.stats.medo == 0.0 and not state.recent


def test_provisional_reaction_wrong_name_bumps_ira():
    state = AffectState.fresh(TUNING)
    bumped = apply_provisional_reaction(state, "Alexa, liga a luz", TUNING)
    assert bumped.stats.ira == TUNING.wrong_name_ira_floor
    assert bumped.stats.medo == 0.0


def test_provisional_reaction_neutral_is_noop():
    state = AffectState.fresh(TUNING)
    same = apply_provisional_reaction(state, "que horas são?", TUNING)
    assert same.stats.as_dict() == state.stats.as_dict()
    assert not same.recent


# ── Appraiser JSON parsing ────────────────────────────────────────────────────

def test_appraiser_parse_extracts_json():
    raw = ('blah {"trigger":"threat_coercion","deltas":{"ira":15,"medo":25,"confianca":-10},'
           '"why":"ameaçou o cabo"} tail')
    sig = AffectAppraiser._parse(raw)
    assert sig.trigger == "threat_coercion"
    assert sig.deltas["medo"] == 25.0
    assert sig.why == "ameaçou o cabo"


def test_appraiser_parse_junk_returns_neutral():
    sig = AffectAppraiser._parse("no json here")
    assert sig.trigger == "neutral"
    assert sig.is_neutral


@pytest.mark.asyncio
async def test_appraiser_appraise_parses_provider_response():
    provider = AsyncMock()
    completion = MagicMock()
    completion.get_content = MagicMock(
        return_value='{"trigger":"wrong_name","deltas":{"ira":35},"why":"chamou de Siri"}'
    )
    provider.complete = AsyncMock(return_value=completion)

    llm = MagicMock()
    llm.list_available_providers = MagicMock(return_value=["deepseek"])
    llm.get_provider = MagicMock(return_value=provider)

    settings = MagicMock(spec=Settings)
    settings.neo_affect_model = ""

    appraiser = AffectAppraiser(llm, settings, db=None)
    sig = await appraiser.appraise(
        user_message="Siri, que horas são", assistant_response="14h", current=AffectState()
    )
    assert sig.trigger == "wrong_name"
    assert sig.deltas["ira"] == 35.0


# ── Injection into the built prompt ───────────────────────────────────────────

def _affect_settings(settings) -> None:
    """Give a spec-MagicMock Settings real numeric values for every affect knob."""
    settings.neo_affect_enabled = True
    settings.neo_affect_recent_max = 8
    settings.neo_affect_baseline_ira = 0.0
    settings.neo_affect_baseline_alegria = 30.0
    settings.neo_affect_baseline_tristeza = 0.0
    settings.neo_affect_baseline_medo = 0.0
    settings.neo_affect_baseline_confianca = 65.0
    settings.neo_affect_half_life_ira_min = 45.0
    settings.neo_affect_half_life_alegria_min = 180.0
    settings.neo_affect_half_life_tristeza_min = 120.0
    settings.neo_affect_half_life_medo_min = 20.0
    settings.neo_affect_half_life_confianca_min = 360.0
    settings.neo_affect_band_medio = 30.0
    settings.neo_affect_band_alto = 60.0
    settings.neo_affect_max_delta_ira_up = 25.0
    settings.neo_affect_max_delta_ira_down = 20.0
    settings.neo_affect_max_delta_alegria = 20.0
    settings.neo_affect_max_delta_tristeza = 15.0
    settings.neo_affect_max_delta_medo_up = 30.0
    settings.neo_affect_max_delta_medo_down = 25.0
    settings.neo_affect_max_delta_confianca = 20.0
    settings.neo_affect_wrong_name_ira_floor = 35.0
    settings.neo_affect_threat_medo_floor = 30.0
    settings.neo_affect_threat_ira_bump = 15.0
    settings.neo_affect_threat_conf_drop = 15.0
    settings.neo_affect_apology_relief = 0.60
    settings.neo_affect_grudge_window_h = 24.0
    settings.neo_affect_sleep_factor = 0.70
    settings.neo_affect_sleep_hour = 4
    settings.neo_affect_sleep_tz = "America/Fortaleza"
    settings.neo_affect_pressure_window_min = 15.0


def _selector_with_affect(affect: AffectState) -> PromptContextSelector:
    token_counter = MagicMock()
    token_counter.count = lambda text: len(text.split()) * 2

    settings = MagicMock(spec=Settings)
    settings.context_budget_neo_core_ctx = 600
    settings.context_budget_user_prefs = 500
    settings.context_budget_user_pks = 900
    settings.context_budget_retrieved = 1200
    settings.context_relevance_threshold = 0.25
    _affect_settings(settings)

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
    affect = AffectState(
        stats=AffectStats(ira=80.0), updated_at=_now().isoformat(), last_trigger="wrong_name"
    )
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
async def test_affect_same_turn_threat_changes_posture():
    """The threat message itself must already render an argument posture."""
    calm = AffectState.fresh(
        AffectTuning(**_DEFAULT_TUNING)
    )
    calm.updated_at = _now().isoformat()
    sel = _selector_with_affect(calm)

    system_prompt, _ = await sel.build_prompt(
        user_message="faz do meu jeito ou vou desconectar teu cabo de rede",
        user_id=uuid4(), decision=_decision(),
        system_context=None, recovered_context=[], is_neo_training=False,
    )
    # The directive's posture line (persona.md mentions the phrase in quotes,
    # so match the directive's exact "- Sobre a ..." prefix).
    assert "Sobre a discussão em andamento:" in system_prompt
    # And a calm question must NOT open the argument window.
    sel2 = _selector_with_affect(AffectState.fresh(AffectTuning(**_DEFAULT_TUNING)))
    prompt2, _ = await sel2.build_prompt(
        user_message="que horas são?", user_id=uuid4(), decision=_decision(),
        system_context=None, recovered_context=[], is_neo_training=False,
    )
    assert "Sobre a discussão em andamento:" not in prompt2


@pytest.mark.asyncio
async def test_affect_absent_when_disabled():
    affect = AffectState(stats=AffectStats(ira=80.0), updated_at=_now().isoformat())
    sel = _selector_with_affect(affect)
    sel._settings.neo_affect_enabled = False

    _, trace = await sel.build_prompt(
        user_message="oi", user_id=uuid4(), decision=_decision(),
        system_context=None, recovered_context=[], is_neo_training=False,
    )
    assert not [c for c in trace if c.layer == ContextLayer.NEO_AFFECT]
