"""Fixtures do Modo Birra — estado emocional montado à mão, sem DB e sem LLM."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.agent.affect import AffectState, AffectStats, AffectTuning
from shared.config import Settings


def now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def tuning() -> AffectTuning:
    return AffectTuning.from_settings(Settings())


@pytest.fixture
def settings() -> Settings:
    """Settings com a birra LIGADA — o default de produção é off."""
    return Settings(neo_birra_enabled=True)


def make_state(
    *,
    ira: float = 0.0,
    alegria: float = 30.0,
    tristeza: float = 0.0,
    medo: float = 0.0,
    confianca: float = 65.0,
    offenses: int = 0,
    pressure_trigger: str | None = None,
    age_min: float = 0.0,
) -> AffectState:
    """Estado pronto para decisão, com rancor e janela de pressão opcionais."""
    stamp = now() - timedelta(minutes=age_min)
    state = AffectState(
        stats=AffectStats(
            ira=ira, alegria=alegria, tristeza=tristeza, medo=medo, confianca=confianca
        ),
        updated_at=stamp.isoformat(),
    )
    if offenses:
        state.grudge = {"count": offenses, "last_at": stamp.isoformat()}
    if pressure_trigger:
        state.recent = [
            {"t": stamp.isoformat(), "trigger": pressure_trigger, "deltas": {}, "why": "teste"}
        ]
    return state
