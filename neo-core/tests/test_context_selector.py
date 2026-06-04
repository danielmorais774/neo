"""Tests for PromptContextSelector — selection logic, budget, threshold, ordering."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core.agent.context_selector import PromptContextSelector
from shared.config import Settings
from shared.types import ContextLayer, MemorySearchResult, RoutingDecision, IntentType


def _make_selector(
    *,
    budget_neo_core_ctx: int = 600,
    budget_user_prefs: int = 500,
    budget_user_pks: int = 900,
    budget_retrieved: int = 1200,
    threshold: float = 0.25,
    pks=None,
    prefs=None,
) -> PromptContextSelector:
    """Build a selector with a fake TokenCounter and configurable settings."""
    token_counter = MagicMock()
    token_counter.count = lambda text: len(text.split()) * 2  # rough estimate

    settings = MagicMock(spec=Settings)
    settings.context_budget_neo_core_ctx = budget_neo_core_ctx
    settings.context_budget_user_prefs = budget_user_prefs
    settings.context_budget_user_pks = budget_user_pks
    settings.context_budget_retrieved = budget_retrieved
    settings.context_relevance_threshold = threshold

    return PromptContextSelector(
        token_counter=token_counter,
        settings=settings,
        pks=pks,
        preference_extractor=prefs,
    )


def _make_decision(**kwargs) -> RoutingDecision:
    defaults = dict(
        intent=IntentType.DIRECT_RESPONSE,
        confidence=0.9,
        needs_memory=True,
        knowledge_keywords=None,
    )
    defaults.update(kwargs)
    return RoutingDecision(**defaults)


# ── 1. Pin candidates always included regardless of budget ────────────────────

@pytest.mark.asyncio
async def test_pinned_candidates_always_included():
    sel = _make_selector(budget_user_pks=0, budget_user_prefs=0)

    pks_mock = AsyncMock()
    pks_mock.retrieve_system_context = AsyncMock(return_value="Neo personality rules")
    pks_mock.retrieve_context = AsyncMock(return_value=({"candidates": []}, ""))
    sel._pks = pks_mock

    system_prompt, trace = await sel.build_prompt(
        user_message="oi",
        user_id=None,
        decision=_make_decision(),
        system_context="macOS 14",
        recovered_context=[],
        is_neo_training=False,
    )

    pinned = [c for c in trace if c.pin]
    assert all(c.included for c in pinned), "All pinned candidates must be included"
    assert any(c.layer == ContextLayer.CORE for c in pinned)
    assert any(c.layer == ContextLayer.RUNTIME for c in pinned)
    assert any(c.layer == ContextLayer.NEO_CORE_PINNED for c in pinned)


# ── 2. Budget enforcement ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_budget_enforced_for_user_pks():
    """USER_PKS block with est_tokens > budget must be excluded."""
    sel = _make_selector(budget_user_pks=10, threshold=0.0)  # tiny budget

    pks_mock = AsyncMock()
    pks_mock.retrieve_system_context = AsyncMock(return_value="")
    # Return a large context text that exceeds the budget
    large_text = "word " * 200  # ~400 tokens in our fake counter
    pks_mock.retrieve_context = AsyncMock(
        return_value=({"candidates": [{"score": 0.9}]}, large_text)
    )
    sel._pks = pks_mock

    _, trace = await sel.build_prompt(
        user_message="treino",
        user_id=uuid4(),
        decision=_make_decision(knowledge_keywords=["treino"]),
        system_context=None,
        recovered_context=[],
        is_neo_training=False,
    )

    pks_candidates = [c for c in trace if c.layer == ContextLayer.USER_PKS]
    assert pks_candidates, "USER_PKS candidate should exist"
    assert not pks_candidates[0].included
    assert pks_candidates[0].skip_reason == "budget"


# ── 3. Threshold enforcement ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_threshold_filters_low_score_retrieved():
    sel = _make_selector(threshold=0.5, budget_retrieved=9999)

    # Recovered context with low score
    low_score_mem = MemorySearchResult(
        content="irrelevant old memory",
        final_score=0.1,
    )

    _, trace = await sel.build_prompt(
        user_message="qual o tempo?",
        user_id=None,
        decision=_make_decision(),
        system_context=None,
        recovered_context=[low_score_mem],
        is_neo_training=False,
    )

    retrieved = [c for c in trace if c.layer == ContextLayer.RETRIEVED]
    assert retrieved, "RETRIEVED candidate should be created"
    assert not retrieved[0].included
    assert retrieved[0].skip_reason == "below_threshold"


# ── 4. High-score retrieved included ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_high_score_retrieved_included():
    sel = _make_selector(threshold=0.25, budget_retrieved=9999)

    good_mem = MemorySearchResult(content="relevant memory about treino", final_score=0.8)

    _, trace = await sel.build_prompt(
        user_message="treino hoje",
        user_id=None,
        decision=_make_decision(),
        system_context=None,
        recovered_context=[good_mem],
        is_neo_training=False,
    )

    retrieved = [c for c in trace if c.layer == ContextLayer.RETRIEVED]
    assert retrieved
    assert retrieved[0].included


# ── 5. Prompt render ordering ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_render_ordering_core_before_runtime_before_neo_core():
    sel = _make_selector()

    pks_mock = AsyncMock()
    pks_mock.retrieve_system_context = AsyncMock(return_value="neo rules")
    pks_mock.retrieve_context = AsyncMock(return_value=({}, ""))
    sel._pks = pks_mock

    system_prompt, _ = await sel.build_prompt(
        user_message="oi",
        user_id=None,
        decision=_make_decision(),
        system_context="OS: macOS",
        recovered_context=[],
    )

    core_pos = system_prompt.find("Você é Neo")
    runtime_pos = system_prompt.find("OS: macOS")
    neo_core_pos = system_prompt.find("Memória do Neo")

    assert core_pos != -1, "CORE block must appear in prompt"
    assert runtime_pos != -1, "RUNTIME block must appear in prompt"
    assert neo_core_pos != -1, "NEO_CORE_PINNED block must appear in prompt"
    assert core_pos < runtime_pos < neo_core_pos, (
        f"Expected CORE < RUNTIME < NEO_CORE, got {core_pos} < {runtime_pos} < {neo_core_pos}"
    )


# ── 6. Trace completeness ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trace_has_correct_included_flags():
    sel = _make_selector(threshold=0.99, budget_user_pks=0)  # nothing scores above 0.99

    pks_mock = AsyncMock()
    pks_mock.retrieve_system_context = AsyncMock(return_value="")
    pks_mock.retrieve_context = AsyncMock(return_value=({"candidates": [{"score": 0.5}]}, "some knowledge"))
    sel._pks = pks_mock

    _, trace = await sel.build_prompt(
        user_message="test",
        user_id=uuid4(),
        decision=_make_decision(knowledge_keywords=["test"]),
        system_context=None,
        recovered_context=[],
    )

    for c in trace:
        if c.pin:
            assert c.included, f"Pinned {c.key} must be included"
        else:
            # Non-pinned should be excluded (high threshold + zero budget)
            assert not c.included, f"Non-pinned {c.key} should be excluded"
            assert c.skip_reason in ("below_threshold", "budget"), (
                f"Expected skip_reason for {c.key}, got {c.skip_reason!r}"
            )


# ── 7. Neo training blocks user prefs and PKS ─────────────────────────────────

@pytest.mark.asyncio
async def test_neo_training_blocks_user_routes():
    pks_mock = AsyncMock()
    pks_mock.retrieve_system_context = AsyncMock(return_value="neo rules")
    pks_mock.retrieve_context = AsyncMock(return_value=({}, "user knowledge"))

    prefs_mock = AsyncMock()
    prefs_mock.get_preferences_text = AsyncMock(return_value="- Prefer emojis")

    sel = _make_selector(pks=pks_mock, prefs=prefs_mock)

    _, trace = await sel.build_prompt(
        user_message="neo modo treino: ...",
        user_id=uuid4(),
        decision=_make_decision(),
        system_context=None,
        recovered_context=[],
        is_neo_training=True,
    )

    user_layers = [c for c in trace if c.layer in (ContextLayer.USER_PREFS, ContextLayer.USER_PKS)]
    assert not user_layers, "User prefs and PKS must be skipped during Neo training"

    # System context should still be present
    pks_mock.retrieve_system_context.assert_awaited_once()


# ── 8. GLOBAL_TYPES no longer includes routine ───────────────────────────────

def test_global_types_only_health_and_restriction():
    from core.pks.retriever import GLOBAL_TYPES
    # Only hard safety/health constraints should be unconditional.
    # Identity (person) is already in Neo system workspace; workout/goal/prefs
    # should only enter when the query is actually about them.
    assert "routine" not in GLOBAL_TYPES, "routine must not be global (leaks workout into unrelated queries)"
    assert "goal" not in GLOBAL_TYPES, "goal must not be global"
    assert "person" not in GLOBAL_TYPES, "person must not be global (identity already in system workspace)"
    assert "preference" not in GLOBAL_TYPES, "preference must not be global"
    assert "health_state" in GLOBAL_TYPES, "health_state should be global (safety context)"
    assert "restriction" in GLOBAL_TYPES, "restriction should be global (safety context)"
