"""Tests for the situational register (tone-regulation layer, v1).

Covers: the deterministic resolver (full precedence matrix), the gravity
floor/resolution detectors, the sticky GravityTracker decay, the rendered
directive, the affect clamp (register caps applied to render_directive) and
the NEO_REGISTER injection in the PromptContextSelector.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from core.agent.affect import (
    AffectState,
    AffectStats,
    AffectTuning,
    POSTURE_TEXT_ABREVIADA,
    render_directive,
)
from core.agent.register import (
    GRAVITY_ALTA,
    GRAVITY_CRITICA,
    GRAVITY_NORMAL,
    GravityTracker,
    PROFILES,
    Register,
    derive_register,
    detect_gravity_floor,
    detect_resolution_cue,
    max_gravity,
    render_register_directive,
)
from shared.types import IntentType, RoutingDecision


def _decision(**overrides) -> RoutingDecision:
    base = dict(
        intent=IntentType.DIRECT_RESPONSE,
        confidence=0.9,
        speech_act="chitchat",
        tipo_tarefa="simples",
    )
    base.update(overrides)
    return RoutingDecision(**base)


_TUNING = AffectTuning(
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
    sleep_hour=-1,
    sleep_tz="America/Fortaleza",
    pressure_window_min=15.0,
)


# ── max_gravity ───────────────────────────────────────────────────────────────

def test_max_gravity_orders_and_tolerates_junk():
    assert max_gravity("normal", "alta") == GRAVITY_ALTA
    assert max_gravity("critica", "alta", "normal") == GRAVITY_CRITICA
    assert max_gravity(None, "", "whatever") == GRAVITY_NORMAL
    assert max_gravity() == GRAVITY_NORMAL


# ── Gravity floor detector ────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "a produção caiu tudo de uma vez",
    "o sistema tá fora do ar",
    "temos um incidente no servidor",
    "socorro, perdemos os dados",
    "URGENTE: cliente sem acesso",
    "o banco caiu de novo",
    "clientes reclamando que nada abre",
])
def test_gravity_floor_fires_on_live_problems(message):
    assert detect_gravity_floor(message) == GRAVITY_ALTA


@pytest.mark.parametrize("message", [
    "bom dia, Neo",
    "como implemento um b-tree em rust?",
    "qual a capital da Mongólia?",
    "conta uma piada sobre produção de leite",
    "",
])
def test_gravity_floor_stays_quiet_on_normal_turns(message):
    assert detect_gravity_floor(message) == GRAVITY_NORMAL


@pytest.mark.parametrize("message", [
    "resolvido, era o nginx",
    "voltou ao normal aqui",
    "falso alarme, era o meu wifi",
    "normalizou depois do deploy",
])
def test_resolution_cue_detected(message):
    assert detect_resolution_cue(message) is True


def test_resolution_cue_not_on_ordinary_text():
    assert detect_resolution_cue("me lembra de comprar café") is False


# ── GravityTracker (sticky + decay) ───────────────────────────────────────────

def test_tracker_rises_instantly_and_decays_slowly():
    tracker = GravityTracker()
    t0 = datetime.now(timezone.utc)
    assert tracker.bump("s1", GRAVITY_CRITICA, now=t0) == GRAVITY_CRITICA
    # 10 min later: still critica
    assert tracker.current("s1", now=t0 + timedelta(minutes=10)) == GRAVITY_CRITICA
    # 40 min: relaxed to alta
    assert tracker.current("s1", now=t0 + timedelta(minutes=40)) == GRAVITY_ALTA
    # 100 min: back to normal (30 + 60 crossed) and pruned
    assert tracker.current("s1", now=t0 + timedelta(minutes=100)) == GRAVITY_NORMAL
    assert tracker.current("s1", now=t0 + timedelta(minutes=101)) == GRAVITY_NORMAL


def test_tracker_reinforcement_refreshes_clock():
    tracker = GravityTracker()
    t0 = datetime.now(timezone.utc)
    tracker.bump("s1", GRAVITY_ALTA, now=t0)
    # 50 min later another grave turn — clock restarts
    tracker.bump("s1", GRAVITY_ALTA, now=t0 + timedelta(minutes=50))
    assert tracker.current("s1", now=t0 + timedelta(minutes=100)) == GRAVITY_ALTA


def test_tracker_normal_bump_never_lowers():
    tracker = GravityTracker()
    t0 = datetime.now(timezone.utc)
    tracker.bump("s1", GRAVITY_ALTA, now=t0)
    assert tracker.bump("s1", GRAVITY_NORMAL, now=t0 + timedelta(minutes=5)) == GRAVITY_ALTA


def test_tracker_resolve_clears():
    tracker = GravityTracker()
    tracker.bump("s1", GRAVITY_CRITICA)
    tracker.resolve("s1")
    assert tracker.current("s1") == GRAVITY_NORMAL


def test_tracker_sessions_are_independent():
    tracker = GravityTracker()
    tracker.bump("s1", GRAVITY_ALTA)
    assert tracker.current("s2") == GRAVITY_NORMAL


# ── Resolver precedence matrix ────────────────────────────────────────────────

def test_default_is_parceiro():
    register, reason = derive_register(_decision(), user_message="bom dia")
    assert register is Register.PARCEIRO
    assert reason == "default"


def test_skill_register_wins_over_everything():
    register, reason = derive_register(
        _decision(gravity="critica"),
        user_message="a produção caiu",
        skill_register="oficina",
    )
    assert register is Register.OFICINA
    assert reason == "skill_register=oficina"


def test_invalid_skill_register_is_ignored():
    register, _ = derive_register(_decision(), skill_register="turbo")
    assert register is Register.PARCEIRO


def test_gravity_from_classifier_yields_combate():
    register, reason = derive_register(_decision(gravity="alta"), user_message="deu ruim")
    assert register is Register.COMBATE
    assert reason == "gravity=alta"


def test_gravity_floor_yields_combate_even_with_blind_classifier():
    register, _ = derive_register(
        _decision(gravity="normal"), user_message="socorro, a produção caiu"
    )
    assert register is Register.COMBATE


def test_session_gravity_yields_combate():
    register, _ = derive_register(
        _decision(), user_message="e agora?", session_gravity=GRAVITY_ALTA
    )
    assert register is Register.COMBATE


def test_formal_cue_yields_protocolo():
    register, reason = derive_register(
        _decision(), user_message="escreve isso pra eu mandar pro cliente"
    )
    assert register is Register.PROTOCOLO
    assert reason == "formal_cue"


def test_register_hint_protocolo_honored():
    register, reason = derive_register(
        _decision(register_hint="protocolo"), user_message="escreve o email"
    )
    assert register is Register.PROTOCOLO
    assert reason == "register_hint=protocolo"


def test_confide_cue_yields_confidente():
    register, reason = derive_register(
        _decision(), user_message="preciso desabafar, briguei com meu irmão"
    )
    assert register is Register.CONFIDENTE
    assert reason == "confide_cue"


def test_register_hint_confidente_honored():
    register, _ = derive_register(
        _decision(register_hint="confidente"), user_message="hoje foi pesado"
    )
    assert register is Register.CONFIDENTE


def test_register_hint_only_accepts_protocolo_confidente():
    # A rogue hint value can never force other registers.
    register, _ = derive_register(
        _decision(register_hint="combate"), user_message="oi"
    )
    assert register is Register.PARCEIRO


def test_gravity_beats_register_hint():
    register, _ = derive_register(
        _decision(gravity="alta", register_hint="protocolo"),
        user_message="sobe o hotfix",
    )
    assert register is Register.COMBATE


def test_live_simple_imperative_yields_silencioso():
    register, reason = derive_register(
        _decision(speech_act="imperative", intent=IntentType.HOME_ASSISTANT),
        user_message="apaga a luz da sala",
        live=True,
    )
    assert register is Register.SILENCIOSO
    assert reason == "live_imperative_simple"


def test_live_chitchat_is_not_silencioso():
    register, _ = derive_register(
        _decision(speech_act="chitchat"), user_message="bom dia, Neo", live=True
    )
    assert register is Register.PARCEIRO


def test_non_live_imperative_is_not_silencioso():
    register, _ = derive_register(
        _decision(speech_act="imperative"), user_message="apaga a luz", live=False
    )
    assert register is Register.PARCEIRO


def test_multi_step_task_yields_oficina():
    register, reason = derive_register(
        _decision(tipo_tarefa="multi_etapas"), user_message="migra o banco"
    )
    assert register is Register.OFICINA
    assert reason == "tipo_tarefa=multi_etapas"


def test_client_tool_yields_oficina():
    register, _ = derive_register(
        _decision(intent=IntentType.CLIENT_TOOL, speech_act="imperative"),
        user_message="lê o arquivo ~/Downloads/x.pdf",
    )
    assert register is Register.OFICINA


def test_skill_hint_yields_oficina():
    register, reason = derive_register(
        _decision(skill_hint="music"), user_message="toca aquela do queen"
    )
    assert register is Register.OFICINA
    assert reason == "skill_hint=music"


def test_server_tool_simple_question_stays_parceiro():
    register, _ = derive_register(
        _decision(intent=IntentType.SERVER_TOOL, speech_act="question"),
        user_message="qual o clima em Fortaleza?",
    )
    assert register is Register.PARCEIRO


# ── Directive render ──────────────────────────────────────────────────────────

def test_directive_contains_header_and_profile_text():
    for register in Register:
        text = render_register_directive(register)
        assert text.startswith("## Registro da situação")
        assert "TETO" in text
        assert PROFILES[register].directive in text
        # The invariant: form, never content.
        assert "nunca o conteúdo" in text


# ── Affect clamp (caps) ───────────────────────────────────────────────────────

def _calm_state(alegria: float = 30.0) -> AffectState:
    return AffectState(stats=AffectStats(alegria=alegria))


def test_no_caps_is_byte_identical_to_legacy():
    state = AffectState(stats=AffectStats(ira=70.0, alegria=55.0))
    assert render_directive(state, _TUNING) == render_directive(state, _TUNING, caps=None)


def test_combate_suppresses_joy_and_lightness():
    # Very happy Neo + incident register → no playful lines at all.
    state = _calm_state(alegria=85.0)
    directive = render_directive(state, _TUNING, caps=PROFILES[Register.COMBATE])
    assert "brincalhão" not in directive
    assert "zoeira leve" not in directive
    assert "Humor geral: leve." not in directive
    # The register's own calm variant renders instead.
    assert "foco total" in directive


def test_protocolo_keeps_anger_but_drops_mockery():
    # Angry + amused (deboche_afiado combo) under protocolo: ira renders,
    # the mocking combo does not.
    state = AffectState(stats=AffectStats(ira=45.0, alegria=55.0))
    plain = render_directive(state, _TUNING)
    assert "Deboche afiado" in plain  # sanity: combo fires without caps
    clamped = render_directive(state, _TUNING, caps=PROFILES[Register.PROTOCOLO])
    assert "Deboche afiado" not in clamped
    assert "irritando" in clamped or "puto" in clamped  # ira line survives


def test_silencioso_calm_renders_nothing():
    directive = render_directive(_calm_state(), _TUNING, caps=PROFILES[Register.SILENCIOSO])
    assert directive == ""


def test_parceiro_caps_keep_full_joy():
    state = _calm_state(alegria=85.0)
    plain = render_directive(state, _TUNING)
    capped = render_directive(state, _TUNING, caps=PROFILES[Register.PARCEIRO])
    assert capped == plain


def test_oficina_attenuates_high_joy():
    state = _calm_state(alegria=85.0)
    directive = render_directive(state, _TUNING, caps=PROFILES[Register.OFICINA])
    assert "alfinetada curta" in directive
    assert "brincalhão" not in directive


def test_negative_emotions_always_render():
    state = AffectState(stats=AffectStats(tristeza=70.0))
    for register in Register:
        directive = render_directive(state, _TUNING, caps=PROFILES[register])
        assert "desanimado" in directive


def test_combate_abbreviates_posture():
    now = datetime.now(timezone.utc)
    state = AffectState(
        stats=AffectStats(ira=70.0, confianca=80.0),
        recent=[{"t": now.isoformat(), "trigger": "contested", "deltas": {}, "why": "discordou"}],
    )
    plain = render_directive(state, _TUNING, now=now)
    assert "dobre a aposta" in plain  # sanity
    clamped = render_directive(state, _TUNING, caps=PROFILES[Register.COMBATE], now=now)
    assert POSTURE_TEXT_ABREVIADA in clamped
    assert "dobre a aposta" not in clamped


def test_protocolo_posture_is_contained():
    now = datetime.now(timezone.utc)
    state = AffectState(
        stats=AffectStats(ira=70.0, confianca=80.0),
        recent=[{"t": now.isoformat(), "trigger": "contested", "deltas": {}, "why": "discordou"}],
    )
    clamped = render_directive(state, _TUNING, caps=PROFILES[Register.PROTOCOLO], now=now)
    assert "frieza educada" in clamped
    assert "deboche" not in clamped


def test_low_leveza_registers_drop_recent_reason():
    now = datetime.now(timezone.utc)
    state = AffectState(
        stats=AffectStats(ira=45.0),
        recent=[{"t": now.isoformat(), "trigger": "insult", "deltas": {}, "why": "xingou o Neo"}],
    )
    plain = render_directive(state, _TUNING, now=now)
    assert "Motivo recente" in plain
    protocolo = render_directive(state, _TUNING, caps=PROFILES[Register.PROTOCOLO], now=now)
    assert "Motivo recente" not in protocolo
    combate = render_directive(state, _TUNING, caps=PROFILES[Register.COMBATE], now=now)
    assert "Motivo recente" in combate  # leveza_max=1 keeps it


# ── Context selector injection ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_selector_injects_register_layer_before_affect():
    from core.agent.context_selector import PromptContextSelector
    from shared.config import Settings
    from shared.types import ContextLayer

    settings = Settings(neo_affect_enabled=False)
    tc = MagicMock()
    tc.count.side_effect = lambda text: len(text) // 4
    selector = PromptContextSelector(tc, settings)

    prompt, candidates = await selector.build_prompt(
        user_message="a produção caiu",
        user_id=None,
        decision=_decision(),
        system_context=None,
        recovered_context=[],
        register=Register.COMBATE,
    )
    layers = [c.layer for c in candidates if c.included]
    assert ContextLayer.NEO_REGISTER in layers
    assert "## Registro da situação" in prompt
    assert PROFILES[Register.COMBATE].directive in prompt


@pytest.mark.asyncio
async def test_selector_without_register_is_unchanged():
    from core.agent.context_selector import PromptContextSelector
    from shared.config import Settings
    from shared.types import ContextLayer

    settings = Settings(neo_affect_enabled=False)
    tc = MagicMock()
    tc.count.side_effect = lambda text: len(text) // 4
    selector = PromptContextSelector(tc, settings)

    prompt, candidates = await selector.build_prompt(
        user_message="oi",
        user_id=None,
        decision=_decision(),
        system_context=None,
        recovered_context=[],
    )
    assert all(c.layer is not ContextLayer.NEO_REGISTER for c in candidates)
    # O persona.md cita a seção pelo nome; o que não pode existir sem registro
    # é a SEÇÃO injetada (o header markdown).
    assert "## Registro da situação" not in prompt
