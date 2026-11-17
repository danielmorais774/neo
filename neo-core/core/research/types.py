"""Tipos e protocolos do ResearchAgent — desacoplamento por injeção.

REGRA DURA (docs/deep-research-v2-plan.md §2): este pacote não importa
``core/jobs``, ``modules/*``, ``db/*`` nem ``api/*`` — só stdlib. As dependências
(LLM barato, LLM forte, busca, progresso) entram por estes protocolos,
implementados por adapters no lado do chamador (``core/jobs/runner_research.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ResearchError(Exception):
    """Falha limpa do pipeline (ex.: LLM devolveu JSON inválido 2x no PLAN)."""


# ---------------------------------------------------------------------------
# Dados de busca/leitura (espelham SearchResult/FetchedPage sem importar modules/)
# ---------------------------------------------------------------------------

@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str
    published_at: str | None = None
    source: str | None = None


@dataclass
class PageContent:
    url: str
    title: str
    text: str
    published_at: str | None = None


@dataclass
class PageError:
    url: str
    error: str


@dataclass
class Source:
    n: int
    title: str
    url: str
    published_at: str | None = None


# ---------------------------------------------------------------------------
# Plano (v2): requisitos → sub-perguntas → outline → queries
# ---------------------------------------------------------------------------

@dataclass
class Requirement:
    """Requisito explícito extraído do pedido (INTAKE) — checável no VALIDATE."""

    id: str
    text: str
    # min_sources | section_required | period | format | other
    check: str = "other"
    value: object = None

    def to_dict(self) -> dict:
        return {"id": self.id, "text": self.text, "check": self.check, "value": self.value}


@dataclass
class RequirementSet:
    explicit: list[Requirement] = field(default_factory=list)
    implicit_scope: str = ""
    language: str = "pt-BR"

    def min_sources(self) -> int | None:
        for req in self.explicit:
            if req.check == "min_sources":
                try:
                    return int(req.value)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
        return None

    def required_sections(self) -> list[str]:
        return [
            str(r.value)
            for r in self.explicit
            if r.check == "section_required" and r.value
        ]

    def to_dict(self) -> dict:
        return {
            "explicit": [r.to_dict() for r in self.explicit],
            "implicit_scope": self.implicit_scope,
            "language": self.language,
        }


@dataclass
class SubQuestion:
    id: str
    question: str


@dataclass
class OutlineSection:
    section: str
    covers: list[str] = field(default_factory=list)  # ids de sub-perguntas


@dataclass
class ResearchPlan:
    title: str
    sub_questions: list[SubQuestion] = field(default_factory=list)
    outline: list[OutlineSection] = field(default_factory=list)
    queries: list[dict] = field(default_factory=list)  # {query, category, recency, sub_question}

    def body_sections(self) -> list[OutlineSection]:
        """Seções de corpo — Resumo executivo e Conclusão são montados à parte."""
        skip = {"resumo executivo", "conclusão", "conclusao", "fontes"}
        return [s for s in self.outline if s.section.strip().lower() not in skip]


# ---------------------------------------------------------------------------
# Evidência (ledger) — substitui as "notas em linha" da v1
# ---------------------------------------------------------------------------

@dataclass
class Evidence:
    id: str
    claim: str
    quote: str = ""
    url: str = ""
    published_at: str | None = None
    # academic | official | news | encyclopedia | blog | other
    source_type: str = "other"
    stance: str | None = None
    sub_question_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "claim": self.claim,
            "quote": self.quote,
            "url": self.url,
            "published_at": self.published_at,
            "source_type": self.source_type,
            "stance": self.stance,
            "sub_question_id": self.sub_question_id,
        }


@dataclass
class SectionCluster:
    """Saída do ORGANIZE: evidências e relações por seção do outline."""

    section: str
    evidence_ids: list[str] = field(default_factory=list)
    convergences: list[str] = field(default_factory=list)
    divergences: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Qualidade (VALIDATE + CRITIC)
# ---------------------------------------------------------------------------

@dataclass
class ValidationFailure:
    check: str
    detail: str
    section: str | None = None

    def to_dict(self) -> dict:
        return {"check": self.check, "detail": self.detail, "section": self.section}


@dataclass
class ValidationResult:
    passed: bool = True
    failures: list[ValidationFailure] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "failures": [f.to_dict() for f in self.failures]}

    def failed_checks(self) -> set[str]:
        return {f.check for f in self.failures}


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------

@dataclass
class ResearchStats:
    queries: int = 0
    searches: int = 0
    fetches: int = 0
    sources_used: int = 0
    llm_calls: int = 0
    duration_s: float = 0.0
    rounds: int = 0
    evidence_count: int = 0
    revisions: int = 0
    writer_model: str = ""
    # Chamadas que voltaram vazias (orçamento consumido no raciocínio do modelo)
    # e precisaram de retentativa com teto maior. Se subir, o teto está curto.
    empty_retries: int = 0
    # Fontes que RENDERAM evidência. Comparar com sources_used (as citadas) mostra
    # quanta matéria-prima coletada o relatório deixou na mesa.
    sources_with_evidence: int = 0

    def to_dict(self) -> dict:
        return {
            "queries": self.queries,
            "searches": self.searches,
            "fetches": self.fetches,
            "sources_used": self.sources_used,
            "llm_calls": self.llm_calls,
            "duration_s": round(self.duration_s, 1),
            "rounds": self.rounds,
            "evidence_count": self.evidence_count,
            "revisions": self.revisions,
            "writer_model": self.writer_model,
            "empty_retries": self.empty_retries,
            "sources_with_evidence": self.sources_with_evidence,
        }


@dataclass
class ResearchReport:
    markdown: str
    sources: list[Source] = field(default_factory=list)
    stats: ResearchStats = field(default_factory=ResearchStats)
    # {validation, rubric, issues, revised, coverage}
    quality: dict = field(default_factory=dict)
    requirements: dict = field(default_factory=dict)


@dataclass
class Budget:
    max_queries: int
    max_fetches: int
    max_rounds: int
    timeout_s: int
    fetch_concurrency: int = 4
    max_tokens_synthesis: int = 4000
    # v2
    max_tokens_section: int = 2200
    revision_cycles: int = 1
    min_section_citations: int = 2
    write_concurrency: int = 3


# ---------------------------------------------------------------------------
# Protocolos injetados
# ---------------------------------------------------------------------------

class ResearchLLM(Protocol):
    """Adapter fecha sobre LLMRouter + um modelo (research_model ou o writer)."""

    async def complete(self, system: str, user: str, *, max_tokens: int = 1500) -> str: ...


class ResearchSearch(Protocol):
    """Adapter fecha sobre WebSearchExecutor (search + fetch)."""

    async def search(
        self, query: str, *, recency: str, category: str, n: int
    ) -> list[SearchHit]: ...

    async def fetch(self, url: str, *, max_chars: int) -> PageContent | PageError: ...


class ResearchProgress(Protocol):
    """Adapter escreve fase/metadados na research_task + progress_note do job."""

    async def phase(self, status: str, note: str) -> None: ...

    async def log(self, line: str) -> None: ...

    async def meta(self, key: str, value: dict) -> None: ...
