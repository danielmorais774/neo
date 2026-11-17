"""ResearchTask model — pesquisa profunda em background (deep_research).

1:1 com um ``agent_job`` de ``kind="research"``: o job é a fonte da verdade do
CICLO DE VIDA (claim/timeout/cancel/dispatcher); esta tabela guarda a FASE fina
(planning/searching/reading/synthesizing/validating/revising) para o painel e o
resultado (relatório markdown + fontes + stats + qualidade).
Ver docs/web-search-deep-research-plan.md §4 e docs/deep-research-v2-plan.md §4.1.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, String, Text, Uuid as SAUuid, JSON
from sqlmodel import Field, SQLModel

# Fases terminais (espelham as do agent_job).
RESEARCH_TERMINAL_STATUSES = frozenset({"completed", "failed", "timeout", "cancelled"})


class ResearchTask(SQLModel, table=True):
    __tablename__ = "research_task"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # 1:1 com agent_job.id (ciclo de vida).
    job_id: uuid.UUID = Field(
        sa_column=Column("job_id", SAUuid, nullable=False, unique=True, index=True),
    )
    user_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column("user_id", SAUuid, nullable=True, index=True),
    )
    session_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column("session_id", SAUuid, nullable=True),
    )
    topic: str = Field(sa_column=Column("topic", Text, nullable=False))
    # Direcionamento extra do usuário (opcional).
    instructions: Optional[str] = Field(
        default=None,
        sa_column=Column("instructions", Text, nullable=True),
    )
    # quick | standard | deep
    depth: str = Field(
        default="standard",
        sa_column=Column("depth", String(16), nullable=False),
    )
    # Fase fina: queued | planning | searching | reading | synthesizing |
    # validating | revising | completed | failed | timeout | cancelled
    status: str = Field(
        default="queued",
        sa_column=Column("status", String(32), nullable=False, index=True),
    )
    # Modelo LLM usado em todas as chamadas do research (ex.: deepseek-flash).
    model: str = Field(
        default="",
        sa_column=Column("model", String(64), nullable=False),
    )
    # Backend de busca predominante (searxng | ddgs).
    backend: Optional[str] = Field(
        default=None,
        sa_column=Column("backend", String(32), nullable=True),
    )
    # {queries, searches, fetches, sources_used, llm_calls, duration_s, rounds,
    #  evidence_count, revisions, writer_model}
    stats_json: dict = Field(
        default_factory=dict,
        sa_column=Column("stats_json", JSON, nullable=False),
    )
    # Requisitos explícitos extraídos do pedido (INTAKE) — o que o VALIDATE cobra.
    # {explicit: [{id, text, check, value}], implicit_scope, language}
    requirements_json: dict = Field(
        default_factory=dict,
        sa_column=Column("requirements_json", JSON, nullable=False, server_default="{}"),
    )
    # Qualidade do relatório: {validation, coverage, rubric, issues, revised}.
    # Coluna própria porque stats_json é sobrescrito pelo runner no terminal.
    quality_json: dict = Field(
        default_factory=dict,
        sa_column=Column("quality_json", JSON, nullable=False, server_default="{}"),
    )
    # [{n, title, url, published_at}] — fontes citadas no relatório.
    sources_json: list = Field(
        default_factory=list,
        sa_column=Column("sources_json", JSON, nullable=False),
    )
    # Relatório completo (o painel lê daqui; list não retorna este campo).
    report_markdown: Optional[str] = Field(
        default=None,
        sa_column=Column("report_markdown", Text, nullable=True),
    )
    # Cópia em /workspace/research/<slug>.md (o Neo relê via neo_read_file).
    report_path: Optional[str] = Field(
        default=None,
        sa_column=Column("report_path", String(512), nullable=True),
    )
    error: Optional[str] = Field(
        default=None,
        sa_column=Column("error", Text, nullable=True),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column("created_at", nullable=False, index=True),
    )
    started_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column("started_at", nullable=True),
    )
    finished_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column("finished_at", nullable=True),
    )
