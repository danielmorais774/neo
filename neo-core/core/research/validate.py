"""VALIDATE — checagem determinística do relatório contra o plano e os requisitos.

Custo zero (sem LLM) e é o que mata a classe de falha "o usuário pediu 30
referências e o relatório entregou 23 sem avisar". O CRITIC (modelo forte) cuida
do que é subjetivo; aqui só entra o que dá para conferir contando.
Ver docs/deep-research-v2-plan.md §4.3.
"""

from __future__ import annotations

import re

from core.research.types import (
    Evidence,
    ResearchPlan,
    RequirementSet,
    ValidationFailure,
    ValidationResult,
)

_CITATION_RE = re.compile(r"\[(\d+)\]")


def cited_numbers(markdown: str) -> set[int]:
    """Números de citação [n] usados no CORPO (ignora a seção '## Fontes')."""
    body = markdown.split("## Fontes")[0]
    return {int(m) for m in _CITATION_RE.findall(body)}


def _section_bodies(markdown: str, sections: list[str]) -> dict[str, str]:
    """Fatia o markdown por cabeçalho '## <seção>' (o relatório é montado por nós)."""
    bodies: dict[str, str] = {}
    for section in sections:
        pattern = re.compile(
            r"^##\s+" + re.escape(section) + r"\s*$(.*?)(?=^##\s|\Z)",
            re.MULTILINE | re.DOTALL,
        )
        match = pattern.search(markdown)
        bodies[section] = match.group(1).strip() if match else ""
    return bodies


def validate_report(
    markdown: str,
    plan: ResearchPlan,
    ledger: list[Evidence],
    requirements: RequirementSet,
    valid_sources: set[int],
    *,
    min_section_citations: int = 2,
) -> ValidationResult:
    failures: list[ValidationFailure] = []
    body_sections = [s.section for s in plan.body_sections()]
    bodies = _section_bodies(markdown, body_sections)
    cited = cited_numbers(markdown)

    # 1. Toda seção planejada existe e tem conteúdo.
    for section in body_sections:
        if not bodies.get(section):
            failures.append(
                ValidationFailure(
                    check="section_missing",
                    detail=f"A seção '{section}' do plano não saiu no relatório.",
                    section=section,
                )
            )

    # 2. Citações por seção (uma seção sem citação é opinião, não pesquisa).
    for section in body_sections:
        body = bodies.get(section) or ""
        if not body:
            continue
        n_cited = len({int(m) for m in _CITATION_RE.findall(body)})
        if n_cited < min_section_citations:
            failures.append(
                ValidationFailure(
                    check="section_citations",
                    detail=(
                        f"A seção '{section}' cita {n_cited} fonte(s); o mínimo é "
                        f"{min_section_citations}."
                    ),
                    section=section,
                )
            )

    # 3. Toda citação [n] do corpo aponta para uma fonte real.
    ghosts = sorted(n for n in cited if n not in valid_sources)
    if ghosts:
        failures.append(
            ValidationFailure(
                check="citation_unknown",
                detail=f"Citações sem fonte correspondente: {ghosts}.",
            )
        )

    # 4. Requisito de nº mínimo de referências (o "pediu 30, entregou 23").
    min_sources = requirements.min_sources()
    if min_sources is not None and len(cited) < min_sources:
        failures.append(
            ValidationFailure(
                check="min_sources",
                detail=(
                    f"O usuário pediu pelo menos {min_sources} referências; o "
                    f"relatório cita {len(cited)}."
                ),
            )
        )

    # 5. Seções exigidas nominalmente pelo usuário.
    lower_md = markdown.lower()
    for wanted in requirements.required_sections():
        if wanted.strip().lower() not in lower_md:
            failures.append(
                ValidationFailure(
                    check="section_required",
                    detail=f"O usuário pediu uma seção sobre '{wanted}' e ela não aparece.",
                )
            )

    # 6. Cobertura: sub-pergunta sem nenhuma evidência coletada.
    covered = {e.sub_question_id for e in ledger if e.sub_question_id}
    for sub in plan.sub_questions:
        if sub.id not in covered:
            failures.append(
                ValidationFailure(
                    check="subquestion_uncovered",
                    detail=f"Sem evidência para a sub-pergunta: {sub.question}",
                )
            )

    return ValidationResult(passed=not failures, failures=failures)


def coverage_matrix(plan: ResearchPlan, ledger: list[Evidence]) -> list[dict]:
    """Matriz sub-pergunta × evidência (insumo do REFLECT e do painel)."""
    matrix: list[dict] = []
    for sub in plan.sub_questions:
        items = [e for e in ledger if e.sub_question_id == sub.id]
        types = sorted({e.source_type for e in items})
        matrix.append(
            {
                "id": sub.id,
                "question": sub.question,
                "evidence": len(items),
                "source_types": types,
                "has_strong_source": any(
                    t in ("academic", "official") for t in types
                ),
            }
        )
    orphans = [e for e in ledger if not e.sub_question_id]
    if orphans:
        matrix.append(
            {
                "id": "-",
                "question": "(evidência sem sub-pergunta atribuída)",
                "evidence": len(orphans),
                "source_types": sorted({e.source_type for e in orphans}),
                "has_strong_source": False,
            }
        )
    return matrix
