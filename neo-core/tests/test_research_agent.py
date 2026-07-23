"""ResearchAgent v2 — pipeline puro com fakes dos protocolos (sem rede/DB/LLM).

Cobre o que a v2 promete (docs/deep-research-v2-plan.md §9): plano com outline,
escrita por seção no modelo FORTE, fontes = só as citadas, cap de fetch por
rodada (o loop que na v1 era dead code), REFLECT dirigido a lacunas, validação de
requisitos, crítica/revisão, tiers de fonte e near-dup.
"""

import json

import pytest

from core.research import ResearchAgent, ResearchError
from core.research.source_tiers import source_type_for, tier_for
from core.research.types import Budget, PageContent, PageError, SearchHit


# ── fakes ───────────────────────────────────────────────────────────────────
class _FakeLLM:
    """LLM scriptado por PASSO (o pipeline v2 não é mais uma fila linear).

    ``script`` mapeia um marcador do system prompt → resposta (ou lista de
    respostas consumidas em ordem). Assim os testes não quebram quando um passo
    roda em paralelo ou é pulado.
    """

    def __init__(self, script: dict[str, object], default: str = ""):
        self.script = {k: (list(v) if isinstance(v, list) else v) for k, v in script.items()}
        self.default = default
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system, user, *, max_tokens=1500):
        self.calls.append((system, user))
        for marker, response in self.script.items():
            if marker in system:
                if isinstance(response, list):
                    return response.pop(0) if response else self.default
                return response
        return self.default

    def count(self, marker: str) -> int:
        return sum(1 for system, _ in self.calls if marker in system)


class _FakeSearch:
    def __init__(self, hits_per_query=3, fail_urls=(), urls=None):
        self.hits_per_query = hits_per_query
        self.fail_urls = set(fail_urls)
        self.urls = urls  # lista fixa de URLs (para tiers/near-dup)
        self.search_calls = 0
        self.fetch_calls = 0
        self.fetched_urls: list[str] = []
        self.queries: list[str] = []
        self._counter = 0

    async def search(self, query, *, recency, category, n):
        self.search_calls += 1
        self.queries.append(query)
        if self.urls is not None:
            return [
                SearchHit(title=f"Fonte {i}", url=url, snippet="trecho")
                for i, url in enumerate(self.urls, start=1)
            ]
        hits = []
        for _ in range(self.hits_per_query):
            self._counter += 1
            hits.append(
                SearchHit(
                    title=f"Fonte {self._counter}",
                    url=f"https://fonte{self._counter}.com/artigo",
                    snippet=f"trecho sobre {query}",
                )
            )
        return hits

    async def fetch(self, url, *, max_chars):
        self.fetch_calls += 1
        self.fetched_urls.append(url)
        if url in self.fail_urls:
            return PageError(url=url, error="fetch_failed")
        return PageContent(
            url=url, title=f"Página {url}", text="Fato datado em 2026-07-10. " * 30
        )


class _FakeProgress:
    def __init__(self):
        self.phases: list[str] = []
        self.logs: list[str] = []
        self.metas: dict[str, dict] = {}

    async def phase(self, status, note):
        self.phases.append(status)

    async def log(self, line):
        self.logs.append(line)

    async def meta(self, key, value):
        self.metas[key] = value


# ── scripts ─────────────────────────────────────────────────────────────────
_PLAN_MARKER = "Você PLANEJA uma pesquisa"
_TRIAGE_MARKER = "Você tria resultados"
_EVIDENCE_MARKER = "Você extrai EVIDÊNCIA"
_REFLECT_MARKER = "Você audita a COBERTURA"
_ORGANIZE_MARKER = "Você organiza a evidência"
_SECTION_MARKER = "escrevendo UMA seção"
_CLOSE_MARKER = "escreve o RESUMO EXECUTIVO"
_CRITIC_MARKER = "professor universitário"
_REVISE_MARKER = "Você REESCREVE uma seção"
_INTAKE_MARKER = "Você extrai os REQUISITOS"


def _plan_json(sections=("Causas", "Impactos"), n_queries=2):
    return json.dumps(
        {
            "title": "Relatório de teste",
            "sub_questions": [
                {"id": f"q{i}", "question": f"pergunta {i}"}
                for i in range(1, len(sections) + 1)
            ],
            "outline": [
                {"section": s, "covers": [f"q{i}"]}
                for i, s in enumerate(sections, start=1)
            ],
            "queries": [
                {
                    "query": f"query {i}",
                    "category": "general",
                    "recency": "any",
                    "sub_question": "q1",
                }
                for i in range(1, n_queries + 1)
            ],
        }
    )


def _evidence_json(url="https://fonte1.com/artigo", sub="q1", n=2):
    return json.dumps(
        {
            "evidence": [
                {
                    "claim": f"fato {i} em 2026",
                    "quote": "trecho literal",
                    "url": url,
                    "stance": None,
                    "sub_question": sub,
                }
                for i in range(1, n + 1)
            ]
        }
    )


def _organize_json(sections=("Causas", "Impactos")):
    return json.dumps(
        {
            "sections": [
                {
                    "section": s,
                    "evidence_ids": ["e1", "e2"],
                    "convergences": ["e1+e2: concordam"],
                    "divergences": [],
                    "links": [],
                }
                for s in sections
            ]
        }
    )


_SECTION_BODY = "Análise com evidência cruzada [1] e contraponto [2]."
_CLOSE_BODY = "<<<RESUMO>>>\nAchados [1].\n<<<CONCLUSAO>>>\nConclusão [2]."
_CRITIC_OK = json.dumps({"scores": {"depth": 4, "coherence": 4, "coverage": 4, "evidence": 4}, "issues": []})


def _base_script(**over):
    script = {
        _PLAN_MARKER: _plan_json(),
        _TRIAGE_MARKER: json.dumps({"selected": [{"n": 1, "reason": "boa"}, {"n": 2, "reason": "boa"}]}),
        _EVIDENCE_MARKER: _evidence_json(),
        _ORGANIZE_MARKER: _organize_json(),
        _SECTION_MARKER: _SECTION_BODY,
        _CLOSE_MARKER: _CLOSE_BODY,
        _CRITIC_MARKER: _CRITIC_OK,
        _REFLECT_MARKER: json.dumps({"queries": [], "new_sub_questions": []}),
    }
    script.update(over)
    return script


def _budget(**over) -> Budget:
    base = dict(
        max_queries=4, max_fetches=4, max_rounds=1, timeout_s=60,
        fetch_concurrency=2, revision_cycles=0, min_section_citations=1,
    )
    base.update(over)
    return Budget(**base)


# ── pipeline ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_report_follows_outline_and_cites_only_used_sources():
    # Lê 3 fontes; o texto só cita [1] e [2] → a terceira NÃO entra em '## Fontes'
    # (a v1 listava tudo que baixou, citado ou não).
    llm = _FakeLLM(_base_script(**{
        _TRIAGE_MARKER: json.dumps({"selected": [{"n": n, "reason": "boa"} for n in (1, 2, 3)]}),
    }))
    writer = _FakeLLM(_base_script())
    search = _FakeSearch()
    progress = _FakeProgress()
    agent = ResearchAgent(llm, search, progress, _budget(), writer_llm=writer)

    report = await agent.run("tópico teste")

    # Estrutura montada pelo outline do plano (não pelo capricho do modelo).
    assert report.markdown.startswith("# Relatório de teste")
    assert "## Resumo executivo" in report.markdown
    assert "## Causas" in report.markdown
    assert "## Impactos" in report.markdown
    assert "## Conclusão" in report.markdown
    assert report.markdown.index("## Resumo executivo") < report.markdown.index("## Causas")

    # A escrita foi para o modelo FORTE (uma chamada por seção + fechamento).
    assert writer.count(_SECTION_MARKER) == 2
    assert writer.count(_CLOSE_MARKER) == 1
    assert llm.count(_SECTION_MARKER) == 0

    # Fontes = só as citadas ([1] e [2]), não tudo que foi baixado.
    assert search.fetch_calls == 3
    assert {s.n for s in report.sources} == {1, 2}
    assert report.stats.sources_used == 2
    assert "planning" in progress.phases and "validating" in progress.phases


def test_word_target_fits_inside_the_token_cap():
    """Bug do 1º relatório em prod: o prompt pedia ~820 palavras com teto de 1400
    tokens e as seções saíam cortadas no meio da frase. O alvo tem que caber
    folgado no teto (pt-BR ≈ 2,8 tokens/palavra)."""
    from core.research.agent import _words_for
    from core.research.budgets import BUDGETS

    for depth, budget in BUDGETS.items():
        words = _words_for(budget.max_tokens_section)
        estimated_tokens = words * 2.8
        assert estimated_tokens < budget.max_tokens_section, (
            f"{depth}: alvo de {words} palavras (~{estimated_tokens:.0f} tokens) "
            f"não cabe no teto de {budget.max_tokens_section}"
        )


@pytest.mark.asyncio
async def test_iso_datetime_published_at_is_trimmed_to_the_day():
    class _DatedSearch(_FakeSearch):
        async def fetch(self, url, *, max_chars):
            self.fetch_calls += 1
            return PageContent(
                url=url, title="Artigo", text="conteúdo " * 40,
                published_at="2026-01-01T00:00:00",
            )

    agent = ResearchAgent(
        _FakeLLM(_base_script()), _DatedSearch(), _FakeProgress(), _budget(),
        writer_llm=_FakeLLM(_base_script()),
    )
    report = await agent.run("t")
    assert report.sources[0].published_at == "2026-01-01"


class _EmptyOnceLLM(_FakeLLM):
    """Devolve VAZIO na 1ª chamada de cada marcador (o DeepSeek gasta o orçamento
    raciocinando e volta com content vazio) e o script na 2ª."""

    def __init__(self, script):
        super().__init__(script)
        self.emptied: set[str] = set()
        self.max_tokens_seen: list[int] = []

    async def complete(self, system, user, *, max_tokens=1500):
        self.max_tokens_seen.append(max_tokens)
        marker = next((m for m in self.script if m in system), None)
        if marker and marker not in self.emptied:
            self.emptied.add(marker)
            self.calls.append((system, user))
            return ""
        return await super().complete(system, user, max_tokens=max_tokens)


@pytest.mark.asyncio
async def test_empty_response_is_retried_with_more_headroom():
    """Bug central achado em prod: ~1 de 4 chamadas voltava vazia
    (finish_reason=length, orçamento gasto no raciocínio interno). Silenciosamente
    isso apagava seções do relatório, perdia lotes de evidência e zerava a rubrica."""
    llm = _EmptyOnceLLM(_base_script())
    writer = _EmptyOnceLLM(_base_script())
    agent = ResearchAgent(
        llm, _FakeSearch(), _FakeProgress(), _budget(), writer_llm=writer
    )
    report = await agent.run("t")

    # Nada se perdeu: o relatório tem as duas seções e a evidência.
    assert "## Causas" in report.markdown and "## Impactos" in report.markdown
    assert report.stats.evidence_count >= 1
    assert report.stats.empty_retries >= 1
    # A retentativa veio com teto MAIOR (senão o modelo gasta tudo de novo).
    assert max(writer.max_tokens_seen) > min(writer.max_tokens_seen)


def test_salvage_recovers_complete_items_from_truncated_json():
    """Bug que segurou a evidência em 2 fontes em prod: a extração estourava o
    teto de tokens no meio da lista e o lote INTEIRO (páginas já baixadas) ia
    para o lixo. O que veio completo tem que ser aproveitado."""
    from core.research.agent import _salvage_object_list

    truncated = (
        '{"evidence": [\n'
        '  {"claim": "fato 1", "quote": "q1", "url": "https://a.com", "sub_question": "q1"},\n'
        '  {"claim": "fato 2", "quote": "q2", "url": "https://b.com", "sub_question": "q2"},\n'
        '  {"claim": "fato 3", "quote": "q3 cortado no me'
    )
    items = _salvage_object_list(truncated, "evidence")
    assert [i["claim"] for i in items] == ["fato 1", "fato 2"]
    assert _salvage_object_list("nada de json aqui", "evidence") == []


@pytest.mark.asyncio
async def test_truncated_evidence_batch_is_salvaged_not_dropped():
    truncated = (
        '{"evidence": [{"claim": "fato salvo", "quote": "q", '
        '"url": "https://fonte1.com/artigo", "sub_question": "q1"}, {"claim": "cor'
    )
    llm = _FakeLLM(_base_script(**{_EVIDENCE_MARKER: truncated}))
    agent = ResearchAgent(
        llm, _FakeSearch(), _FakeProgress(), _budget(),
        writer_llm=_FakeLLM(_base_script()),
    )
    report = await agent.run("t")
    assert report.stats.evidence_count >= 1  # o lote não foi perdido
    assert llm.count(_EVIDENCE_MARKER) == 1  # salvage evita até o retry


@pytest.mark.asyncio
async def test_evidence_url_with_trailing_slash_still_matches():
    """URL levemente diferente (barra final) não pode custar a evidência."""
    evidence = json.dumps({
        "evidence": [{
            "claim": "fato", "quote": "q",
            "url": "https://fonte1.com/artigo/",  # o fake serve sem a barra
            "sub_question": "q1",
        }]
    })
    agent = ResearchAgent(
        _FakeLLM(_base_script(**{_EVIDENCE_MARKER: evidence})), _FakeSearch(),
        _FakeProgress(), _budget(), writer_llm=_FakeLLM(_base_script()),
    )
    report = await agent.run("t")
    assert report.stats.evidence_count >= 1


@pytest.mark.asyncio
async def test_evidence_ledger_feeds_stats_and_coverage():
    llm = _FakeLLM(_base_script())
    agent = ResearchAgent(llm, _FakeSearch(), _FakeProgress(), _budget(), writer_llm=_FakeLLM(_base_script()))
    report = await agent.run("t")
    assert report.stats.evidence_count > 0
    assert report.quality["coverage"][0]["evidence"] > 0


@pytest.mark.asyncio
async def test_plan_invalid_json_retries_then_fails_clean():
    llm = _FakeLLM({_PLAN_MARKER: ["não é json", "ainda não é json"]})
    agent = ResearchAgent(llm, _FakeSearch(), _FakeProgress(), _budget())
    with pytest.raises(ResearchError):
        await agent.run("t")
    assert llm.count(_PLAN_MARKER) == 2  # 1 chamada + 1 retry, depois falha limpa


@pytest.mark.asyncio
async def test_truncated_plan_is_salvaged_instead_of_killing_the_research():
    """Uma pesquisa morreu em prod porque o JSON do plano cortou na última query.
    O PLAN é o único passo que aborta — o plano que já veio é utilizável."""
    truncated = (
        '{"title": "Relatório salvo",\n'
        ' "sub_questions": [{"id": "q1", "question": "p1"}],\n'
        ' "outline": [{"section": "Causas", "covers": ["q1"]}],\n'
        ' "queries": [{"query": "q boa", "category": "general", "recency": "any",'
        ' "sub_question": "q1"}, {"query": "cortada no me'
    )
    llm = _FakeLLM(_base_script(**{_PLAN_MARKER: truncated}))
    agent = ResearchAgent(
        llm, _FakeSearch(), _FakeProgress(), _budget(),
        writer_llm=_FakeLLM(_base_script()),
    )
    report = await agent.run("t")
    assert report.markdown.startswith("# Relatório salvo")
    assert "## Causas" in report.markdown
    assert llm.count(_PLAN_MARKER) == 1  # salvage evita até o retry


@pytest.mark.asyncio
async def test_plan_without_outline_fails_even_if_json_is_valid():
    no_outline = json.dumps({"title": "T", "sub_questions": [], "outline": [], "queries": []})
    llm = _FakeLLM({_PLAN_MARKER: no_outline})
    agent = ResearchAgent(llm, _FakeSearch(), _FakeProgress(), _budget())
    with pytest.raises(ResearchError):
        await agent.run("t")


@pytest.mark.asyncio
async def test_no_readable_sources_raises_clean_error():
    llm = _FakeLLM(_base_script())
    agent = ResearchAgent(llm, _FakeSearch(hits_per_query=0), _FakeProgress(), _budget())
    with pytest.raises(ResearchError):
        await agent.run("t")


@pytest.mark.asyncio
async def test_failed_source_is_skipped_without_aborting():
    llm = _FakeLLM(_base_script())
    search = _FakeSearch(fail_urls={"https://fonte1.com/artigo"})
    progress = _FakeProgress()
    agent = ResearchAgent(llm, search, progress, _budget(), writer_llm=_FakeLLM(_base_script()))
    report = await agent.run("t")
    assert report.markdown  # a pesquisa segue com as outras fontes
    assert any("fonte1" in log for log in progress.logs)


@pytest.mark.asyncio
async def test_organize_degrades_to_covers_when_json_invalid():
    """ORGANIZE ruim não pode deixar seção sem material (fallback determinístico)."""
    llm = _FakeLLM(_base_script(**{_ORGANIZE_MARKER: ["lixo", "mais lixo"]}))
    agent = ResearchAgent(llm, _FakeSearch(), _FakeProgress(), _budget(), writer_llm=_FakeLLM(_base_script()))
    report = await agent.run("t")
    assert "## Causas" in report.markdown


# ── loop iterativo (o bug B1 da v1) ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_round_cap_leaves_budget_for_second_round():
    """Na v1 a rodada 1 comia max_fetches inteiro e a rodada 2 nunca acontecia."""
    reflect = json.dumps(
        {
            "queries": [
                {"query": "lacuna X", "category": "general", "recency": "any", "sub_question": "q2"}
            ],
            "new_sub_questions": [],
        }
    )
    llm = _FakeLLM(_base_script(**{
        _REFLECT_MARKER: reflect,
        # Evidência só para q1 → q2 fica descoberta → REFLECT dispara rodada 2.
        _EVIDENCE_MARKER: _evidence_json(sub="q1"),
        _TRIAGE_MARKER: json.dumps({"selected": [{"n": i, "reason": "x"} for i in range(1, 9)]}),
    }))
    search = _FakeSearch(hits_per_query=5)
    progress = _FakeProgress()
    budget = _budget(max_fetches=8, max_rounds=2, max_queries=4)
    agent = ResearchAgent(llm, search, progress, budget, writer_llm=_FakeLLM(_base_script()))

    report = await agent.run("t")

    assert report.stats.rounds == 2, "a rodada 2 tem que acontecer (cap por rodada)"
    assert "lacuna X" in search.queries, "a rodada 2 busca a LACUNA, não uma query genérica"
    assert search.fetch_calls <= budget.max_fetches  # teto global preservado


@pytest.mark.asyncio
async def test_reflect_exits_early_when_coverage_is_complete():
    llm = _FakeLLM(_base_script(**{
        # Evidência forte para as duas sub-perguntas → sem lacuna → sem REFLECT.
        _EVIDENCE_MARKER: json.dumps({
            "evidence": [
                {"claim": "f1", "quote": "q", "url": "https://arxiv.org/abs/1", "sub_question": "q1"},
                {"claim": "f2", "quote": "q", "url": "https://arxiv.org/abs/1", "sub_question": "q2"},
                {"claim": "f3", "quote": "q", "url": "https://arxiv.org/abs/1", "sub_question": "q1"},
                {"claim": "f4", "quote": "q", "url": "https://arxiv.org/abs/1", "sub_question": "q2"},
            ]
        }),
    }))
    search = _FakeSearch(urls=["https://arxiv.org/abs/1", "https://nature.com/x"])
    budget = _budget(max_fetches=8, max_rounds=3)
    agent = ResearchAgent(llm, search, _FakeProgress(), budget, writer_llm=_FakeLLM(_base_script()))
    report = await agent.run("t")
    assert report.stats.rounds == 1
    assert llm.count(_REFLECT_MARKER) == 0  # cobertura ok → nem chama o LLM


# ── requisitos, validação e revisão ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_min_sources_requirement_failure_is_declared_not_hidden():
    intake = json.dumps({
        "explicit": [{"id": "r1", "text": "pelo menos 30 referências", "check": "min_sources", "value": 30}],
        "implicit_scope": "",
        "language": "pt-BR",
    })
    llm = _FakeLLM(_base_script(**{_INTAKE_MARKER: intake}))
    progress = _FakeProgress()
    agent = ResearchAgent(
        llm, _FakeSearch(), progress, _budget(revision_cycles=1),
        writer_llm=_FakeLLM(_base_script()),
    )
    report = await agent.run("t", instructions="use pelo menos 30 referências")

    assert progress.metas["requirements"]["explicit"][0]["check"] == "min_sources"
    validation = report.quality["validation"]
    assert validation["passed"] is False
    assert any(f["check"] == "min_sources" for f in validation["failures"])
    # O relatório DIZ que não cumpriu (a v1 entregava 23/30 calado).
    assert "Nota de cobertura" in report.markdown
    assert "30 referências" in report.markdown


@pytest.mark.asyncio
async def test_uncited_sources_are_used_before_searching_more():
    """Achado do painel: 30 evidências coletadas, 6 fontes citadas. Faltar
    referência por falta de USO é diferente de faltar por falta de fonte — o
    conserto certo é reescrever aproveitando o que já foi lido, não buscar mais."""
    intake = json.dumps({
        "explicit": [{"id": "r1", "text": "pelo menos 2 referências", "check": "min_sources", "value": 2}],
        "implicit_scope": "", "language": "pt-BR",
    })
    # Duas fontes lidas, ambas com evidência (um lote só: batch = 2 páginas).
    evidence = json.dumps({
        "evidence": [
            {"claim": "f1", "quote": "q", "url": "https://fonte1.com/artigo", "sub_question": "q1"},
            {"claim": "f2", "quote": "q", "url": "https://fonte2.com/artigo", "sub_question": "q1"},
        ]
    })
    organize = json.dumps({
        "sections": [
            {"section": "Causas", "evidence_ids": ["e1", "e2"], "convergences": [], "divergences": [], "links": []},
            {"section": "Impactos", "evidence_ids": ["e2"], "convergences": [], "divergences": [], "links": []},
        ]
    })
    llm = _FakeLLM(_base_script(**{
        _PLAN_MARKER: _plan_json(n_queries=1),
        _INTAKE_MARKER: intake, _EVIDENCE_MARKER: evidence, _ORGANIZE_MARKER: organize,
    }))
    # …mas o writer ancora tudo em [1] (inclusive o fechamento); a revisão
    # passa a citar as duas.
    writer = _FakeLLM(_base_script(**{
        _SECTION_MARKER: "Só uma fonte [1].",
        _CLOSE_MARKER: "<<<RESUMO>>>\nAchado [1].\n<<<CONCLUSAO>>>\nFecho [1].",
        _REVISE_MARKER: "Agora cruzando [1] e [2].",
        _CRITIC_MARKER: _CRITIC_OK,
    }))
    search = _FakeSearch(hits_per_query=2)
    progress = _FakeProgress()
    agent = ResearchAgent(
        llm, search, progress,
        _budget(max_fetches=2, revision_cycles=1, min_section_citations=1),
        writer_llm=writer,
    )
    report = await agent.run("t", instructions="use pelo menos 2 referências")

    assert report.stats.sources_with_evidence == 2
    assert writer.count(_REVISE_MARKER) >= 1  # reescreveu para aproveitar a fonte na mesa
    assert any("não citadas" in log for log in progress.logs)
    assert search.search_calls == 1  # não gastou busca nova
    assert "[2]" in report.markdown


@pytest.mark.asyncio
async def test_critic_issue_triggers_targeted_rewrite():
    critic = json.dumps({
        "scores": {"depth": 2, "coherence": 3, "coverage": 3, "evidence": 2},
        "issues": [{"section": "Causas", "issue": "só resume fontes", "fix": "cruze as fontes"}],
    })
    writer = _FakeLLM(_base_script(**{
        _CRITIC_MARKER: critic,
        _REVISE_MARKER: "Versão revisada cruzando [1] e [2].",
    }))
    llm = _FakeLLM(_base_script())
    agent = ResearchAgent(
        llm, _FakeSearch(), _FakeProgress(), _budget(revision_cycles=1),
        writer_llm=writer,
    )
    report = await agent.run("t")

    assert writer.count(_REVISE_MARKER) == 1  # só a seção apontada
    assert "Versão revisada" in report.markdown
    assert report.quality["revised"] is True
    assert report.quality["rubric"]["depth"] == 2
    assert report.stats.revisions == 1


@pytest.mark.asyncio
async def test_quick_depth_skips_critic():
    writer = _FakeLLM(_base_script())
    agent = ResearchAgent(
        _FakeLLM(_base_script()), _FakeSearch(), _FakeProgress(),
        _budget(revision_cycles=0), writer_llm=writer,
    )
    await agent.run("t")
    assert writer.count(_CRITIC_MARKER) == 0  # quick não paga crítica


@pytest.mark.asyncio
async def test_ghost_citation_is_caught_by_validator():
    writer = _FakeLLM(_base_script(**{_SECTION_MARKER: "Afirmação com fonte inexistente [99]."}))
    agent = ResearchAgent(
        _FakeLLM(_base_script()), _FakeSearch(), _FakeProgress(),
        _budget(), writer_llm=writer,
    )
    report = await agent.run("t")
    checks = {f["check"] for f in report.quality["validation"]["failures"]}
    assert "citation_unknown" in checks


# ── fontes: tiers e near-dup ────────────────────────────────────────────────
def test_tier_and_source_type_of_known_domains():
    assert tier_for("https://arxiv.org/abs/2401.001") == 1
    assert tier_for("https://www.ibge.gov.br/estatisticas") == 1
    assert tier_for("https://pt.wikipedia.org/wiki/X") == 2
    assert tier_for("https://blog-do-fulano.com/post") == 3
    assert source_type_for("https://arxiv.org/abs/1") == "academic"
    assert source_type_for("https://www.gov.br/saude") == "official"
    assert source_type_for("https://pt.wikipedia.org/wiki/X") == "encyclopedia"


def test_brazilian_journals_and_repositories_are_academic():
    """Achado do 1º relatório em prod: revista revisada por pares em OJS
    (emnuvens) e repositório institucional caíam em tier 3 / 'other'."""
    assert tier_for("https://revistaaber.emnuvens.com.br/rberu/article/view/1206") == 1
    assert tier_for("https://repositorio.ufpi.br/xmlui/handle/123456789/4089") == 1
    assert tier_for("https://periodicos.ufmg.br/index.php/x") == 1
    assert source_type_for("https://revistaaber.emnuvens.com.br/rberu/article/view/1206") == "academic"
    # Imprensa de grande circulação não vira "acadêmica" por causa do subdomínio.
    assert tier_for("https://g1.globo.com/economia") == 2


@pytest.mark.asyncio
async def test_triage_prefers_tier1_when_llm_json_degrades():
    llm = _FakeLLM(_base_script(**{
        _TRIAGE_MARKER: ["lixo", "lixo"],  # JSON ruim → degrada p/ ordem por tier
        _EVIDENCE_MARKER: _evidence_json(url="https://arxiv.org/abs/123"),
    }))
    search = _FakeSearch(urls=[
        "https://blogueiro.com/a",
        "https://arxiv.org/abs/123",
        "https://outro-blog.com/b",
    ])
    agent = ResearchAgent(llm, search, _FakeProgress(), _budget(max_fetches=2), writer_llm=_FakeLLM(_base_script()))
    await agent.run("t")
    assert search.fetched_urls[0] == "https://arxiv.org/abs/123"  # tier 1 primeiro


@pytest.mark.asyncio
async def test_near_duplicate_titles_are_fetched_once():
    class _DupSearch(_FakeSearch):
        async def search(self, query, *, recency, category, n):
            self.search_calls += 1
            return [
                SearchHit(title="Revolução Industrial: o guia completo", url="https://a.com/1", snippet="s"),
                SearchHit(title="Revolucao Industrial - o guia completo!", url="https://b.com/2", snippet="s"),
            ]

    search = _DupSearch()
    agent = ResearchAgent(
        _FakeLLM(_base_script()), search, _FakeProgress(), _budget(),
        writer_llm=_FakeLLM(_base_script()),
    )
    await agent.run("t")
    assert search.fetch_calls == 1  # a matéria republicada não é lida duas vezes
