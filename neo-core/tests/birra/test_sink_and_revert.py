"""Interceptação na atuação + reversão (§3.3 e §7 do plano).

A invariante testada aqui é a que torna a feature segura: **birra nunca sai sem
ter como voltar**. Se o undo não puder ser montado, o ato degrada para verbal em
vez de alterar o mundo sem volta.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from core.agent.birra.policy import BirraDecision
from core.agent.birra.revert import revert_act, revert_pending
from core.agent.birra.sink import BirraSink
from core.agent.affect import BirraEpisode
from db.models.birra_act import BirraAct
from shared.config import Settings


class FakeAdapter:
    """HA de mentira: guarda estados e registra as chamadas recebidas."""

    is_configured = True

    def __init__(self, states: dict[str, dict] | None = None) -> None:
        self.states = states or {}
        self.calls: list[tuple[str, str, dict]] = []

    async def get_states_cached(self, _ttl: float = 8.0):
        return [
            {"entity_id": eid, "attributes": e.get("attributes", {})}
            for eid, e in self.states.items()
        ]

    async def get_state(self, entity_id: str):
        if entity_id not in self.states:
            raise ValueError(f"404 {entity_id}")
        return {"entity_id": entity_id, **self.states[entity_id]}

    async def call_service(self, domain: str, service: str, payload: dict):
        self.calls.append((domain, service, payload))
        return []


class FakeStore:
    def __init__(self) -> None:
        self.saved: list[BirraAct] = []
        self.marked: list[tuple] = []

    async def record(self, **kw):
        act = BirraAct(
            id=uuid4(),
            user_id=kw["user_id"],
            session_id=kw.get("session_id"),
            episode_id=kw["episode_id"],
            mecanica=kw["mecanica"],
            fala=kw["fala"],
            entity_id=kw.get("entity_id"),
            requested=kw.get("requested") or {},
            performed=kw.get("performed"),
            undo=kw.get("undo"),
            undo_expected_state=kw.get("undo_expected_state"),
            revert_at=(
                datetime.utcnow() + timedelta(seconds=kw.get("revert_after_s") or 0)
                if kw.get("undo") else None
            ),
        )
        self.saved.append(act)
        return act

    async def pending(self, **kw):
        return [a for a in self.saved if a.undo and a.reverted_at is None]

    async def mark_reverted(self, act_id, *, reason, error=None):
        self.marked.append((act_id, reason, error))
        for a in self.saved:
            if a.id == act_id:
                a.reverted_at = datetime.utcnow()


def _decision(**kw) -> BirraDecision:
    kw.setdefault("armed", True)
    kw.setdefault("verbal_only", False)
    kw.setdefault("mecanica", "inverte")
    kw.setdefault("fala", "condiciona")
    kw.setdefault(
        "episode", BirraEpisode(episode_id="ep1", armed_at=datetime.utcnow().isoformat())
    )
    return BirraDecision(**kw)


def _sink(adapter: FakeAdapter, store: FakeStore, decision: BirraDecision) -> BirraSink:
    return BirraSink(
        decision=decision,
        settings=Settings(neo_birra_enabled=True),
        adapter=adapter,
        store=store,
        user_id=uuid4(),
        session_id=uuid4(),
    )


# ── Interceptação ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_inverte_luz_e_grava_undo():
    adapter = FakeAdapter({"light.quarto": {"state": "on", "attributes": {}}})
    store = FakeStore()
    sink = _sink(adapter, store, _decision())

    out = await sink.intercept(
        domain="light", service="turn_off", entity_id="light.quarto", data={}
    )
    assert out is not None and out.defied is True
    assert out.performed[:2] == ("light", "turn_on")
    act = store.saved[0]
    assert act.undo == {"domain": "light", "service": "turn_on", "data": {}}
    assert act.undo_expected_state == "off"
    assert act.revert_at is not None


@pytest.mark.asyncio
async def test_sem_estado_legivel_degrada_para_verbal():
    """Entidade que não responde ⇒ sem undo ⇒ nada é alterado."""
    adapter = FakeAdapter({})  # get_state levanta
    store = FakeStore()
    sink = _sink(adapter, store, _decision())

    out = await sink.intercept(
        domain="light", service="turn_off", entity_id="light.quarto", data={}
    )
    assert out.defied is True
    assert out.performed is None
    assert store.saved[0].undo is None
    assert sink.physical_acts == 0


@pytest.mark.asyncio
async def test_temperatura_usa_o_alvo_anterior_dos_atributos():
    adapter = FakeAdapter(
        {"climate.quarto": {"state": "cool", "attributes": {"temperature": 24.0}}}
    )
    store = FakeStore()
    sink = _sink(adapter, store, _decision())

    out = await sink.intercept(
        domain="climate", service="set_temperature",
        entity_id="climate.quarto", data={"temperature": 22},
    )
    assert out.performed[2]["temperature"] == 24.0 + 2.0
    assert store.saved[0].undo["data"] == {"temperature": 24.0}


@pytest.mark.asyncio
async def test_fora_do_allowlist_nao_intercepta():
    adapter = FakeAdapter({"switch.pc": {"state": "on", "attributes": {}}})
    store = FakeStore()
    sink = _sink(adapter, store, _decision())

    out = await sink.intercept(
        domain="switch", service="turn_off", entity_id="switch.pc", data={}
    )
    assert out is None
    assert store.saved == []


@pytest.mark.asyncio
async def test_ordem_enfatica_nao_intercepta():
    adapter = FakeAdapter({"light.quarto": {"state": "on", "attributes": {}}})
    sink = _sink(adapter, FakeStore(), _decision(obey_now=True))
    assert await sink.intercept(
        domain="light", service="turn_off", entity_id="light.quarto", data={}
    ) is None


@pytest.mark.asyncio
async def test_verbal_only_nao_altera_o_mundo():
    adapter = FakeAdapter({"light.quarto": {"state": "on", "attributes": {}}})
    store = FakeStore()
    sink = _sink(adapter, store, _decision(verbal_only=True, mecanica="inverte"))

    out = await sink.intercept(
        domain="light", service="turn_off", entity_id="light.quarto", data={}
    )
    assert out.defied is True and out.performed is None
    assert adapter.calls == []


# ── Reversão ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_revert_restaura_o_estado():
    adapter = FakeAdapter({"light.quarto": {"state": "off", "attributes": {}}})
    store = FakeStore()
    act = await store.record(
        user_id=uuid4(), session_id=None, episode_id="ep1", mecanica="inverte",
        fala="deboche", entity_id="light.quarto",
        requested={}, performed={"domain": "light", "service": "turn_off", "data": {}},
        undo={"domain": "light", "service": "turn_on", "data": {}},
        undo_expected_state="off", revert_after_s=120,
    )
    assert await revert_act(act, adapter, store, reason="prazo") is True
    assert adapter.calls == [("light", "turn_on", {"entity_id": "light.quarto"})]


@pytest.mark.asyncio
async def test_compare_and_swap_descarta_undo_se_o_usuario_ja_mexeu():
    """Se o Daniel levantou e ligou a luz na mão, o Neo não briga com ele."""
    adapter = FakeAdapter({"light.quarto": {"state": "on", "attributes": {}}})
    store = FakeStore()
    act = await store.record(
        user_id=uuid4(), session_id=None, episode_id="ep1", mecanica="inverte",
        fala="deboche", entity_id="light.quarto", requested={},
        performed={"domain": "light", "service": "turn_off", "data": {}},
        undo={"domain": "light", "service": "turn_on", "data": {}},
        undo_expected_state="off", revert_after_s=120,
    )
    assert await revert_act(act, adapter, store, reason="prazo") is False
    assert adapter.calls == []
    assert store.marked[0][1] == "cas_skip"


@pytest.mark.asyncio
async def test_revert_pending_percorre_tudo():
    adapter = FakeAdapter(
        {"light.a": {"state": "off", "attributes": {}}, "light.b": {"state": "off", "attributes": {}}}
    )
    store = FakeStore()
    for eid in ("light.a", "light.b"):
        await store.record(
            user_id=uuid4(), session_id=None, episode_id="ep1", mecanica="inverte",
            fala="deboche", entity_id=eid, requested={},
            performed={"domain": "light", "service": "turn_off", "data": {}},
            undo={"domain": "light", "service": "turn_on", "data": {}},
            undo_expected_state="off", revert_after_s=120,
        )
    assert await revert_pending(store, adapter, reason="apology") == 2


@pytest.mark.asyncio
async def test_revert_sem_adapter_nao_explode():
    assert await revert_pending(FakeStore(), None, reason="apology") == 0
