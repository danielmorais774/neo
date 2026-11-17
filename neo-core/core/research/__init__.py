"""ResearchAgent — subagente de pesquisa profunda, puro e desacoplado.

Não importa core/jobs, modules/*, db/* nem api/*: tudo entra por protocolos
injetados (LLM barato, LLM forte, busca, progresso).
Ver docs/deep-research-v2-plan.md.
"""

from core.research.agent import ResearchAgent
from core.research.budgets import BUDGETS, DEFAULT_DEPTH, budget_for
from core.research.source_tiers import source_type_for, tier_for
from core.research.types import (
    Budget,
    Evidence,
    OutlineSection,
    PageContent,
    PageError,
    Requirement,
    RequirementSet,
    ResearchError,
    ResearchLLM,
    ResearchPlan,
    ResearchProgress,
    ResearchReport,
    ResearchSearch,
    ResearchStats,
    SearchHit,
    SectionCluster,
    Source,
    SubQuestion,
    ValidationResult,
)
from core.research.validate import coverage_matrix, validate_report

__all__ = [
    "ResearchAgent",
    "BUDGETS",
    "DEFAULT_DEPTH",
    "budget_for",
    "Budget",
    "Evidence",
    "OutlineSection",
    "PageContent",
    "PageError",
    "Requirement",
    "RequirementSet",
    "ResearchError",
    "ResearchLLM",
    "ResearchPlan",
    "ResearchProgress",
    "ResearchReport",
    "ResearchSearch",
    "ResearchStats",
    "SearchHit",
    "SectionCluster",
    "Source",
    "SubQuestion",
    "ValidationResult",
    "coverage_matrix",
    "validate_report",
    "source_type_for",
    "tier_for",
]
