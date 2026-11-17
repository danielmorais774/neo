"""Tests for the AffectUpdater choke point — dedup, timeout fallback, locks, health."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import core.agent.affect_updater as au
from core.agent.affect import AffectSignal, AffectState, AffectTuning
from core.agent.affect_updater import AffectUpdater, get_affect_health
from shared.config import Settings

USER = uuid4()


def _settings(**overrides) -> Settings:
    kwargs = {
        "neo_affect_enabled": True,
        "neo_affect_appraisal_timeout_s": 0.2,
        **overrides,
    }
    return Settings(_env_file=None, **kwargs)


class FakePKS:
    """Records update calls; simulates a slow write to expose interleaving."""

    def __init__(self, write_delay: float = 0.0):
        self.state = AffectState.fresh(AffectTuning.from_settings(_settings()))
        self.updates: list[dict] = []
        self.events: list[str] = []
        self._delay = write_delay

    async def get_user_affect(self, *, user_id):
        return self.state

    async def update_user_affect(self, *, user_id, signal, wrong_name=False,
                                 threat=False, source_message_id=None):
        self.events.append("enter")
        if self._delay:
            await asyncio.sleep(self._delay)
        self.updates.append({
            "signal": signal, "wrong_name": wrong_name, "threat": threat,
            "source_message_id": source_message_id,
        })
        self.events.append("exit")
        return self.state


class FakeAppraiser:
    def __init__(self, signal: AffectSignal | None = None, delay: float = 0.0,
                 error: Exception | None = None):
        self._signal = signal or AffectSignal()
        self._delay = delay
        self._error = error
        self.calls: list[dict] = []

    async def appraise(self, *, user_message, assistant_response, current):
        self.calls.append({"user_message": user_message,
                           "assistant_response": assistant_response})
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return self._signal


@pytest.fixture(autouse=True)
def _reset_health():
    au._health.update(appraisals=0, updates=0, failures=0, last_error=None,
                      last_error_at=None, last_update_at=None)
    au.set_shared_updater(None)
    yield
    au.set_shared_updater(None)


def _updater(pks: FakePKS, appraiser: FakeAppraiser, settings: Settings | None = None) -> AffectUpdater:
    updater = AffectUpdater(MagicMock(), settings or _settings(), pks=pks)
    updater._appraiser = appraiser
    return updater


# ── Gating + dedup ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_schedule_noop_when_disabled_or_incomplete():
    pks = FakePKS()
    updater = _updater(pks, FakeAppraiser(), _settings(neo_affect_enabled=False))
    assert updater.schedule(user_message="oi", assistant_response="x", user_id=USER) is False

    updater2 = _updater(pks, FakeAppraiser())
    assert updater2.schedule(user_message="", assistant_response="x", user_id=USER) is False
    assert updater2.schedule(user_message="oi", assistant_response="x", user_id=None) is False


@pytest.mark.asyncio
async def test_schedule_dedups_by_source_message_id():
    pks = FakePKS()
    appraiser = FakeAppraiser(AffectSignal(trigger="insult", deltas={"ira": 10}))
    updater = _updater(pks, appraiser)

    assert updater.schedule(user_message="seu lixo", assistant_response="…",
                            user_id=USER, source_message_id="m1") is True
    # Second path scheduling the same turn (e.g. epilogue + legacy) is dropped.
    assert updater.schedule(user_message="seu lixo", assistant_response="…",
                            user_id=USER, source_message_id="m1") is False
    await updater.drain()
    assert len(pks.updates) == 1


# ── Appraisal outcomes ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_neutral_turn_skips_write():
    pks = FakePKS()
    updater = _updater(pks, FakeAppraiser(AffectSignal()))  # neutral
    updater.schedule(user_message="que horas são?", assistant_response="14h", user_id=USER)
    await updater.drain()
    assert pks.updates == []


@pytest.mark.asyncio
async def test_timeout_still_persists_deterministic_floors():
    """Appraiser hangs past the cap → floors fire anyway; failure is recorded."""
    pks = FakePKS()
    slow = FakeAppraiser(AffectSignal(trigger="threat_coercion", deltas={"medo": 30}), delay=5.0)
    updater = _updater(pks, slow, _settings(neo_affect_appraisal_timeout_s=0.05))

    updater.schedule(user_message="vou desconectar teu cabo de rede",
                     assistant_response="tenta", user_id=USER)
    await updater.drain(timeout=2.0)

    assert len(pks.updates) == 1
    assert pks.updates[0]["threat"] is True
    assert pks.updates[0]["signal"].is_neutral  # LLM nuance lost, reaction kept
    health = get_affect_health()
    assert health["failures"] == 1 and health["updates"] == 1
    assert "Timeout" in (health["last_error"] or "")


@pytest.mark.asyncio
async def test_appraiser_exception_recorded_and_neutral_turn_dropped():
    pks = FakePKS()
    updater = _updater(pks, FakeAppraiser(error=RuntimeError("boom")))
    updater.schedule(user_message="me conta uma curiosidade", assistant_response="…", user_id=USER)
    await updater.drain()
    assert pks.updates == []  # no detector hit + no signal → nothing to write
    assert get_affect_health()["failures"] == 1


@pytest.mark.asyncio
async def test_internal_error_reply_not_fed_to_appraiser():
    pks = FakePKS()
    appraiser = FakeAppraiser(AffectSignal(trigger="insult", deltas={"ira": 10}))
    updater = _updater(pks, appraiser)
    updater.schedule(user_message="seu inútil",
                     assistant_response="Erro ao processar: boom", user_id=USER)
    await updater.drain()
    assert appraiser.calls[0]["assistant_response"] == ""


# ── Concurrency ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_per_user_lock_serializes_updates():
    pks = FakePKS(write_delay=0.03)
    appraiser = FakeAppraiser(AffectSignal(trigger="insult", deltas={"ira": 10}))
    updater = _updater(pks, appraiser)

    updater.schedule(user_message="msg um, seu lixo", assistant_response="…",
                     user_id=USER, source_message_id="a")
    updater.schedule(user_message="msg dois, seu lixo", assistant_response="…",
                     user_id=USER, source_message_id="b")
    await updater.drain(timeout=2.0)

    assert len(pks.updates) == 2
    # Writes never interleave: enter/exit strictly paired.
    assert pks.events == ["enter", "exit", "enter", "exit"]


@pytest.mark.asyncio
async def test_health_counts_updates():
    pks = FakePKS()
    updater = _updater(pks, FakeAppraiser(AffectSignal(trigger="compliment",
                                                       deltas={"alegria": 10, "ira": -5})))
    updater.schedule(user_message="mandou bem demais", assistant_response="…", user_id=USER)
    await updater.drain()
    health = get_affect_health()
    assert health["appraisals"] == 1 and health["updates"] == 1 and health["failures"] == 0
    assert health["last_update_at"] is not None
    assert health["pending_tasks"] == 0
