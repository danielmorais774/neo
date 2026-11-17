"""Budgets por profundidade — teto duro de queries/fetches/rounds/tempo/revisões.

Timeouts subiram na v2 (pipeline mais fundo: writer por seção + validate/critic
+ revise). Ver docs/deep-research-v2-plan.md §3.4.
"""

from core.research.types import Budget

BUDGETS: dict[str, Budget] = {
    "quick": Budget(
        max_queries=4, max_fetches=6, max_rounds=1, timeout_s=300,
        max_tokens_section=2000, revision_cycles=0, min_section_citations=1,
    ),
    "standard": Budget(
        max_queries=8, max_fetches=12, max_rounds=2, timeout_s=1200,
        max_tokens_section=3000, revision_cycles=1, min_section_citations=2,
    ),
    "deep": Budget(
        max_queries=16, max_fetches=24, max_rounds=4, timeout_s=2700,
        max_tokens_section=3600, revision_cycles=1, min_section_citations=2,
    ),
}

DEFAULT_DEPTH = "standard"


def budget_for(depth: str) -> Budget:
    return BUDGETS.get(depth, BUDGETS[DEFAULT_DEPTH])
