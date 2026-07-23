"""VALIDATE — checagem determinística do relatório (custo zero, sem LLM).

É o que impede o "pediu 30 referências, entregou 23 calado" e o "[9]" que não
existe. Ver docs/deep-research-v2-plan.md §4.3.
"""

from core.research.types import (
    Evidence,
    OutlineSection,
    Requirement,
    RequirementSet,
    ResearchPlan,
    SubQuestion,
)
from core.research.validate import cited_numbers, coverage_matrix, validate_report


def _plan() -> ResearchPlan:
    return ResearchPlan(
        title="T",
        sub_questions=[SubQuestion(id="q1", question="p1"), SubQuestion(id="q2", question="p2")],
        outline=[
            OutlineSection(section="Causas", covers=["q1"]),
            OutlineSection(section="Impactos", covers=["q2"]),
        ],
        queries=[],
    )


def _ledger() -> list[Evidence]:
    return [
        Evidence(id="e1", claim="c1", url="https://a.com", sub_question_id="q1"),
        Evidence(id="e2", claim="c2", url="https://b.com", sub_question_id="q2"),
    ]


_GOOD = """# T

## Causas
Análise cruzada [1] e [2].

## Impactos
Consequência [1][2].

## Fontes
[1] A — https://a.com
[2] B — https://b.com
"""


def test_good_report_passes():
    result = validate_report(_GOOD, _plan(), _ledger(), RequirementSet(), {1, 2})
    assert result.passed
    assert result.failures == []


def test_citations_ignore_the_sources_section():
    # [1]/[2] da seção '## Fontes' não contam como citação de corpo.
    assert cited_numbers(_GOOD) == {1, 2}


def test_missing_section_and_thin_citations_are_caught():
    report = "# T\n\n## Causas\nSó uma fonte [1].\n\n## Fontes\n[1] A — https://a.com\n"
    result = validate_report(report, _plan(), _ledger(), RequirementSet(), {1, 2})
    checks = {(f.check, f.section) for f in result.failures}
    assert ("section_missing", "Impactos") in checks
    assert ("section_citations", "Causas") in checks  # 1 < mínimo 2
    assert not result.passed


def test_min_sources_requirement_is_enforced():
    requirements = RequirementSet(
        explicit=[Requirement(id="r1", text="pelo menos 30 referências", check="min_sources", value=30)]
    )
    result = validate_report(_GOOD, _plan(), _ledger(), requirements, {1, 2})
    failure = next(f for f in result.failures if f.check == "min_sources")
    assert "30" in failure.detail and "2" in failure.detail


def test_ghost_citation_is_caught():
    report = _GOOD.replace("Consequência [1][2].", "Consequência [1][9].")
    result = validate_report(report, _plan(), _ledger(), RequirementSet(), {1, 2})
    assert any(f.check == "citation_unknown" for f in result.failures)


def test_uncovered_subquestion_is_caught():
    ledger = [Evidence(id="e1", claim="c1", url="https://a.com", sub_question_id="q1")]
    result = validate_report(_GOOD, _plan(), ledger, RequirementSet(), {1, 2})
    assert any(f.check == "subquestion_uncovered" for f in result.failures)


def test_required_section_by_name():
    requirements = RequirementSet(
        explicit=[
            Requirement(id="r1", text="inclua uma seção sobre meio ambiente",
                        check="section_required", value="meio ambiente")
        ]
    )
    result = validate_report(_GOOD, _plan(), _ledger(), requirements, {1, 2})
    assert any(f.check == "section_required" for f in result.failures)


def test_coverage_matrix_flags_strong_sources():
    ledger = [
        Evidence(id="e1", claim="c", url="https://arxiv.org/1", source_type="academic", sub_question_id="q1"),
        Evidence(id="e2", claim="c", url="https://blog.com", source_type="other", sub_question_id="q2"),
    ]
    matrix = coverage_matrix(_plan(), ledger)
    by_id = {row["id"]: row for row in matrix}
    assert by_id["q1"]["has_strong_source"] is True
    assert by_id["q2"]["has_strong_source"] is False
