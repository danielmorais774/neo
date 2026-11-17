"""ResearchAgent v2 — pipeline de pesquisa que sintetiza em vez de resumir.

    INTAKE → PLAN → [SEARCH → TRIAGE → FETCH → EVIDENCE → REFLECT]×N
           → ORGANIZE → WRITE(seção a seção) → VALIDATE → CRITIC → REVISE

Duas cabeças: ``llm`` (barato) faz o volume — planejar, triar, extrair evidência,
auditar cobertura, organizar; ``writer`` (forte) escreve, critica e revisa. É
onde a qualidade mora e são poucas chamadas.

Função pura de orquestração: dado fakes dos protocolos (LLM, busca, progresso),
roda inteira em teste sem rede/DB/LLM. O timeout duro fica FORA (o runner envolve
``run()`` em ``asyncio.wait_for(budget.timeout_s)``).
Ver docs/deep-research-v2-plan.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from datetime import date
from difflib import SequenceMatcher

from core.research.prompts import (
    JSON_RETRY_INSTRUCTION,
    citation_goal_note,
    critic_prompts,
    evidence_prompts,
    intake_prompts,
    organize_prompts,
    plan_prompts,
    reflect_prompts,
    revise_prompts,
    section_prompts,
    summary_conclusion_prompts,
    triage_prompts,
)
from core.research.source_tiers import TIER_ACADEMIC, domain_of, source_type_for, tier_for
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
)
from core.research.validate import coverage_matrix, cited_numbers, validate_report

logger = logging.getLogger(__name__)

# Lote de 2 (era 3): a saída da extração é verbosa (claim + citação textual por
# item) e o JSON estourava o teto de tokens no meio da lista.
_EVIDENCE_BATCH_SIZE = 2
_EVIDENCE_CONCURRENCY = 4
_EVIDENCE_MAX_TOKENS = 4000
_PLAN_MAX_TOKENS = 3000  # sub-perguntas + outline + queries num JSON só
_CRITIC_MAX_TOKENS = 3500
_MAX_TOKENS_CEILING = 8000  # teto da retentativa por resposta vazia
_CRITIC_REPORT_CHARS = 24_000  # relatório longo comprime o orçamento da crítica
_FETCH_MAX_CHARS = 6000
_FETCH_MAX_CHARS_TIER1 = 20_000  # paper/dado primário merece ser lido inteiro
_MAX_PER_DOMAIN = 2
_NEAR_DUP_RATIO = 0.85
_NEAR_DUP_MIN_TITLE = 25  # títulos curtos não sustentam comparação por similaridade
_MAX_LEDGER_FOR_ORGANIZE = 120
_SUMMARY_MARKER = "<<<RESUMO>>>"
_CONCLUSION_MARKER = "<<<CONCLUSAO>>>"
_RESERVED_SECTIONS = ("Resumo executivo", "Conclusão")


def _parse_json_object(raw: str) -> dict:
    """Parse defensivo: aceita JSON puro ou cercado de texto/markdown."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.strip())
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        obj = json.loads(match.group(0))
        if isinstance(obj, dict):
            return obj
    raise json.JSONDecodeError("no JSON object found", raw[:200], 0)


def _parse_json_lenient(raw: str, list_keys: tuple[str, ...]) -> dict:
    """Parse que sobrevive a truncamento: recupera as listas conhecidas + o title.

    O PLAN é o único passo que mata a pesquisa, então ele não pode morrer por um
    JSON cortado no último item da última lista — o plano que já veio é
    perfeitamente utilizável.
    """
    try:
        return _parse_json_object(raw)
    except json.JSONDecodeError:
        pass
    out: dict = {}
    for key in list_keys:
        items = _salvage_object_list(raw or "", key)
        if items:
            out[key] = items
    title = re.search(r'"title"\s*:\s*"([^"]{1,300})"', raw or "")
    if title:
        out["title"] = title.group(1)
    if not out:
        raise json.JSONDecodeError("no salvageable JSON", (raw or "")[:200], 0)
    return out


def _salvage_object_list(raw: str, key: str) -> list[dict]:
    """Recupera os objetos COMPLETOS de uma lista JSON que veio truncada.

    Quando o modelo bate no teto de tokens no meio da lista, o JSON fica
    inválido e o lote inteiro (páginas já baixadas e pagas) ia para o lixo — foi
    o que segurou a evidência em 2 fontes no primeiro relatório em prod. Aqui o
    que veio inteiro é aproveitado e só a cauda cortada é descartada.
    """
    start = raw.find(f'"{key}"')
    if start == -1:
        return []
    bracket = raw.find("[", start)
    if bracket == -1:
        return []
    decoder = json.JSONDecoder()
    items: list[dict] = []
    i = bracket + 1
    while i < len(raw):
        while i < len(raw) and raw[i] in ", \n\r\t":
            i += 1
        if i >= len(raw) or raw[i] != "{":
            break
        try:
            obj, end = decoder.raw_decode(raw, i)
        except json.JSONDecodeError:
            break  # objeto cortado no meio: acabou o que dava para salvar
        if isinstance(obj, dict):
            items.append(obj)
        i = end
    return items


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (title or "").lower()).strip()


def _norm_date(raw: str | None) -> str | None:
    """Datas de busca vêm como ISO datetime ('2026-01-01T00:00:00') — só o dia importa."""
    if not raw:
        return None
    match = re.match(r"(\d{4}-\d{2}-\d{2})", str(raw).strip())
    return match.group(1) if match else str(raw).strip()[:40]


def _words_for(max_tokens: int) -> int:
    """Alvo de palavras a partir do teto de tokens.

    pt-BR gasta ~2,8 tokens por palavra nos tokenizers atuais (acentuação e
    palavras longas). O alvo pedido no prompt tem que caber FOLGADO no teto,
    senão o modelo escreve até bater no limite e a seção sai cortada no meio da
    frase — foi o que aconteceu no primeiro relatório em prod.
    """
    return max(180, int(max_tokens / 3.2))


class ResearchAgent:
    """Executa uma pesquisa completa dentro do budget dado."""

    def __init__(
        self,
        llm: ResearchLLM,
        search: ResearchSearch,
        progress: ResearchProgress,
        budget: Budget,
        writer_llm: ResearchLLM | None = None,
    ) -> None:
        self.llm = llm  # barato: plan/triage/evidence/reflect/organize
        self.writer = writer_llm or llm  # forte: write/critic/revise
        self.search = search
        self.progress = progress
        self.budget = budget
        self.stats = ResearchStats()

        # Estado da coleta (compartilhado entre rodadas e a mini-rodada da revisão)
        self._pages: list[PageContent] = []
        self._sources: dict[str, Source] = {}  # url → Source (numeração estável)
        self._ledger: list[Evidence] = []
        self._fetch_attempts = 0
        self._evidence_seq = 0
        self._searched: set[str] = set()

    # ------------------------------------------------------------------
    # Orquestração
    # ------------------------------------------------------------------

    async def run(self, topic: str, instructions: str | None = None) -> ResearchReport:
        t0 = time.monotonic()
        today = date.today().isoformat()

        requirements = await self._intake(topic, instructions)
        if requirements.explicit:
            await self.progress.meta("requirements", requirements.to_dict())

        await self.progress.phase("planning", "Interpretando o pedido e planejando")
        plan = await self._plan(topic, instructions, requirements, today)
        self.stats.queries = len(plan.queries)
        await self.progress.log(
            f"Plano: {len(plan.sub_questions)} sub-pergunta(s), "
            f"{len(plan.body_sections())} seção(ões), {len(plan.queries)} query(ies)"
        )

        await self._collect(topic, plan, plan.queries, today)
        if not self._ledger:
            if not self._pages:
                raise ResearchError(
                    "Nenhuma fonte legível encontrada — as buscas não retornaram páginas úteis."
                )
            raise ResearchError(
                "As fontes lidas não produziram nenhuma evidência utilizável sobre o tópico."
            )

        await self.progress.phase("synthesizing", "Organizando a evidência e redigindo")
        clusters = await self._organize(topic, plan, self._ledger)
        drafts = await self._write_sections(
            topic, plan, clusters, instructions, requirements.min_sources()
        )
        summary, conclusion = await self._summary_and_conclusion(
            topic, plan, drafts, instructions, requirements, today
        )
        report_md = self._assemble(plan, drafts, summary, conclusion)

        report_md, quality = await self._validate_and_revise(
            topic, plan, clusters, drafts, summary, conclusion,
            report_md, requirements, instructions, today,
        )

        sources = self._cited_sources(report_md)
        self.stats.sources_used = len(sources)
        self.stats.evidence_count = len(self._ledger)
        self.stats.sources_with_evidence = self._sources_with_evidence()
        self.stats.duration_s = time.monotonic() - t0
        return ResearchReport(
            markdown=report_md,
            sources=sources,
            stats=self.stats,
            quality=quality,
            requirements=requirements.to_dict(),
        )

    # ------------------------------------------------------------------
    # LLM helpers
    # ------------------------------------------------------------------

    async def _complete(
        self, llm: ResearchLLM, system: str, user: str, *, max_tokens: int
    ) -> str:
        """Chamada com folga para o raciocínio interno do modelo.

        Os DeepSeek gastam parte do orçamento raciocinando ANTES de responder: em
        ~1 de 4 chamadas o teto acaba durante o raciocínio e o content volta
        VAZIO (finish_reason=length). Resposta vazia não é "o modelo não tem o
        que dizer" — é orçamento curto. Uma retentativa com o dobro do teto
        resolve, e sem isso o efeito é devastador e silencioso: seção do
        relatório que some, lote de evidência perdido, rubrica em branco.
        """
        raw = await llm.complete(system, user, max_tokens=max_tokens)
        if (raw or "").strip():
            return raw
        bigger = min(max_tokens * 2, _MAX_TOKENS_CEILING)
        logger.warning(
            "[Research] resposta vazia (orçamento consumido no raciocínio) — "
            "retentando com max_tokens=%d",
            bigger,
        )
        self.stats.llm_calls += 1
        self.stats.empty_retries += 1
        return await llm.complete(system, user, max_tokens=bigger) or ""

    async def _llm_json(
        self, system: str, user: str, *, max_tokens: int, strong: bool = False,
        list_keys: tuple[str, ...] = (),
    ) -> dict:
        """1 chamada + 1 retry com instrução de correção → ResearchError.

        ``list_keys`` liga o parse tolerante a truncamento: uma resposta cortada
        no meio da lista ainda entrega os itens completos, em vez de custar a
        chamada inteira (modo de falha real em prod, ver docs §12.2).
        """
        llm = self.writer if strong else self.llm
        last_error: Exception | None = None
        for attempt in (1, 2):
            self.stats.llm_calls += 1
            prompt = user if attempt == 1 else f"{user}\n\n{JSON_RETRY_INSTRUCTION}"
            raw = await self._complete(llm, system, prompt, max_tokens=max_tokens)
            try:
                if list_keys:
                    return _parse_json_lenient(raw, list_keys)
                return _parse_json_object(raw)
            except json.JSONDecodeError as e:
                last_error = e
        raise ResearchError(f"LLM devolveu JSON inválido 2x: {last_error}")

    async def _llm_json_soft(
        self, system: str, user: str, *, max_tokens: int, step: str, strong: bool = False,
        list_keys: tuple[str, ...] = (),
    ) -> dict | None:
        """Passo opcional: JSON ruim degrada (log) em vez de matar a pesquisa."""
        try:
            return await self._llm_json(
                system, user, max_tokens=max_tokens, strong=strong, list_keys=list_keys
            )
        except ResearchError:
            logger.warning("[Research] %s degradou (JSON inválido 2x)", step)
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning("[Research] %s falhou: %s", step, e)
            return None

    async def _llm_plan_json(self, system: str, user: str) -> dict:
        """PLAN: parse tolerante a truncamento + 1 retry. Só morre se nada vier.

        É o único passo que aborta a pesquisa — não pode morrer porque o JSON
        cortou no último item da última lista (foi o que derrubou uma pesquisa
        em prod depois que o prompt do plano ficou mais exigente).
        """
        keys = ("sub_questions", "outline", "queries")
        for attempt in (1, 2):
            self.stats.llm_calls += 1
            prompt = user if attempt == 1 else f"{user}\n\n{JSON_RETRY_INSTRUCTION}"
            raw = await self._complete(self.llm, system, prompt, max_tokens=_PLAN_MAX_TOKENS)
            try:
                obj = _parse_json_lenient(raw, keys)
            except json.JSONDecodeError:
                continue
            if obj.get("outline") and obj.get("queries"):
                return obj
            logger.warning(
                "[Research] PLAN incompleto (tentativa %d): outline=%d, queries=%d",
                attempt, len(obj.get("outline") or []), len(obj.get("queries") or []),
            )
        raise ResearchError("PLAN não produziu outline e queries utilizáveis (2 tentativas).")

    async def _llm_json_list(
        self, system: str, user: str, *, key: str, max_tokens: int, step: str
    ) -> list[dict]:
        """Passo cuja saída é uma LISTA: tolera truncamento (salvage) antes de retry.

        Resposta cortada no teto de tokens é o caso comum aqui (evidência com
        citação textual é verbosa). Perder o lote inteiro custa páginas já
        baixadas — então primeiro aproveitamos os itens completos.
        """
        self.stats.llm_calls += 1
        raw = await self._complete(self.llm, system, user, max_tokens=max_tokens)
        try:
            obj = _parse_json_object(raw)
            items = obj.get(key)
            if isinstance(items, list):
                return [i for i in items if isinstance(i, dict)]
        except json.JSONDecodeError:
            pass

        salvaged = _salvage_object_list(raw or "", key)
        if salvaged:
            logger.info(
                "[Research] %s: JSON truncado — %d item(ns) recuperado(s)", step, len(salvaged)
            )
            return salvaged

        self.stats.llm_calls += 1
        raw = await self._complete(
            self.llm, system, f"{user}\n\n{JSON_RETRY_INSTRUCTION}", max_tokens=max_tokens
        )
        try:
            obj = _parse_json_object(raw)
            items = obj.get(key)
            if isinstance(items, list):
                return [i for i in items if isinstance(i, dict)]
        except json.JSONDecodeError:
            pass
        salvaged = _salvage_object_list(raw or "", key)
        if not salvaged:
            logger.warning("[Research] %s degradou (JSON inválido 2x)", step)
        return salvaged

    async def _llm_text(
        self, system: str, user: str, *, max_tokens: int, strong: bool = True
    ) -> str:
        llm = self.writer if strong else self.llm
        self.stats.llm_calls += 1
        return (await self._complete(llm, system, user, max_tokens=max_tokens)).strip()

    # ------------------------------------------------------------------
    # 0. INTAKE
    # ------------------------------------------------------------------

    async def _intake(self, topic: str, instructions: str | None) -> RequirementSet:
        if not instructions or not instructions.strip():
            return RequirementSet()  # sem pedido extra não há requisito a extrair
        system, user = intake_prompts(topic, instructions)
        obj = await self._llm_json_soft(
            system, user, max_tokens=800, step="intake", list_keys=("explicit",)
        )
        if not obj:
            return RequirementSet()
        explicit: list[Requirement] = []
        for i, item in enumerate(obj.get("explicit") or [], start=1):
            if not isinstance(item, dict) or not item.get("text"):
                continue
            check = str(item.get("check") or "other")
            if check not in ("min_sources", "section_required", "period", "format", "other"):
                check = "other"
            explicit.append(
                Requirement(
                    id=str(item.get("id") or f"r{i}"),
                    text=str(item["text"]),
                    check=check,
                    value=item.get("value"),
                )
            )
        return RequirementSet(
            explicit=explicit,
            implicit_scope=str(obj.get("implicit_scope") or ""),
            language=str(obj.get("language") or "pt-BR"),
        )

    # ------------------------------------------------------------------
    # 1. PLAN
    # ------------------------------------------------------------------

    async def _plan(
        self,
        topic: str,
        instructions: str | None,
        requirements: RequirementSet,
        today: str,
    ) -> ResearchPlan:
        max_sections = 4 if self.budget.max_rounds <= 1 else 7
        system, user = plan_prompts(
            topic,
            instructions,
            self._requirements_block(requirements),
            self.budget.max_queries,
            max_sections,
            today,
        )
        obj = await self._llm_plan_json(system, user)

        sub_questions: list[SubQuestion] = []
        for i, item in enumerate(obj.get("sub_questions") or [], start=1):
            if isinstance(item, dict) and item.get("question"):
                sub_questions.append(
                    SubQuestion(
                        id=str(item.get("id") or f"q{i}"),
                        question=str(item["question"]).strip(),
                    )
                )
        outline: list[OutlineSection] = []
        for item in obj.get("outline") or []:
            if not isinstance(item, dict) or not item.get("section"):
                continue
            name = str(item["section"]).strip()
            if name.lower() in {s.lower() for s in _RESERVED_SECTIONS}:
                continue  # resumo/conclusão são montados fora
            covers = [str(c) for c in (item.get("covers") or []) if c]
            outline.append(OutlineSection(section=name, covers=covers))
        outline = outline[:max_sections]

        queries = self._parse_queries(obj.get("queries"), self.budget.max_queries)

        if not outline:
            raise ResearchError("PLAN não produziu um outline utilizável.")
        if not queries:
            raise ResearchError("PLAN não produziu nenhuma query utilizável.")
        if not sub_questions:
            # Sem sub-perguntas explícitas o resto do pipeline (cobertura, reflect,
            # validate) perde o eixo — deriva uma por seção antes de seguir.
            sub_questions = [
                SubQuestion(id=f"q{i}", question=f"O que se sabe sobre: {s.section}?")
                for i, s in enumerate(outline, start=1)
            ]
            for i, section in enumerate(outline, start=1):
                section.covers = section.covers or [f"q{i}"]

        title = str(obj.get("title") or "").strip() or f"Pesquisa: {topic}"
        return ResearchPlan(
            title=title, sub_questions=sub_questions, outline=outline, queries=queries
        )

    def _parse_queries(self, raw, limit: int) -> list[dict]:
        queries: list[dict] = []
        for item in (raw or [])[:limit]:
            if not isinstance(item, dict):
                continue
            q = str(item.get("query") or "").strip()
            if not q:
                continue
            category = item.get("category")
            recency = item.get("recency")
            queries.append(
                {
                    "query": q,
                    "category": category if category in ("news", "general", "science") else "general",
                    "recency": recency if recency in ("day", "week", "month", "any") else "any",
                    "sub_question": str(item.get("sub_question") or "") or None,
                }
            )
        return queries

    # ------------------------------------------------------------------
    # 2. LOOP DE COLETA
    # ------------------------------------------------------------------

    async def _collect(
        self, topic: str, plan: ResearchPlan, queries: list[dict], today: str
    ) -> None:
        """Rodadas de busca→leitura→evidência→reflexão, com cap POR RODADA.

        O v1 gastava ``max_fetches`` inteiro na rodada 1 e as rodadas seguintes
        morriam sem orçamento — o loop existia mas nunca rodava. Aqui cada rodada
        tem sua fatia e o que sobra é carregado para a próxima.
        """
        round_cap = max(4, math.ceil(self.budget.max_fetches / max(1, self.budget.max_rounds)))
        carry = 0
        pending = queries
        round_idx = 0

        while pending and round_idx < self.budget.max_rounds:
            remaining_global = self.budget.max_fetches - self._fetch_attempts
            if remaining_global <= 0:
                break
            round_idx += 1
            self.stats.rounds = round_idx

            await self.progress.phase(
                "searching", f"Rodada {round_idx}: {len(pending)} busca(s)"
            )
            hits = await self._search_all(pending)

            allowance = min(round_cap + carry, remaining_global)
            selected = await self._triage(topic, hits, allowance)
            if not selected:
                await self.progress.log(f"Rodada {round_idx}: triage sem fontes novas")
                break
            carry = max(0, (round_cap + carry) - len(selected))

            await self.progress.phase("reading", f"Lendo {len(selected)} fonte(s)")
            pages = await self._fetch_all(selected)
            if not pages:
                await self.progress.log(f"Rodada {round_idx}: nenhuma fonte legível")
                break

            new_evidence = await self._extract_evidence(topic, plan, pages)
            self._ledger.extend(new_evidence)
            await self.progress.log(
                f"Rodada {round_idx}: {len(pages)} fonte(s) lidas, "
                f"{len(new_evidence)} evidência(s) extraída(s)"
            )

            if round_idx >= self.budget.max_rounds:
                break
            if self.budget.max_fetches - self._fetch_attempts <= 0:
                break
            pending = await self._reflect(topic, plan, today)
            if pending:
                await self.progress.log(
                    f"Lacunas de cobertura → {len(pending)} nova(s) busca(s) dirigida(s)"
                )

    async def _search_all(self, queries: list[dict]) -> list[SearchHit]:
        async def one(spec: dict) -> list[SearchHit]:
            key = f"{spec['query']}|{spec.get('category')}|{spec.get('recency')}"
            if key in self._searched:
                return []
            self._searched.add(key)
            try:
                hits = await self.search.search(
                    spec["query"],
                    recency=spec.get("recency", "any"),
                    category=spec.get("category", "general"),
                    n=5,
                )
                self.stats.searches += 1
                return hits
            except Exception as e:  # noqa: BLE001 — busca que falha não derruba a rodada
                logger.warning("[Research] busca '%s' falhou: %s", spec.get("query"), e)
                return []

        results = await asyncio.gather(*(one(q) for q in queries))
        return [hit for batch in results for hit in batch]

    def _dedup_candidates(self, hits: list[SearchHit]) -> list[SearchHit]:
        """Dedup por URL + near-dup por título (mesma matéria republicada)."""
        unique: dict[str, SearchHit] = {}
        for hit in hits:
            if not hit.url or hit.url in self._sources or hit.url in unique:
                continue
            unique[hit.url] = hit

        kept: list[SearchHit] = []
        for hit in unique.values():
            norm = _norm_title(hit.title)
            duplicate_of = None
            # Título curto não é sinal confiável de duplicata ("Fonte 1" e
            # "Fonte 2" ficam a um caractere de distância) — só compara títulos
            # com corpo suficiente.
            if len(norm) >= _NEAR_DUP_MIN_TITLE:
                for i, other in enumerate(kept):
                    other_norm = _norm_title(other.title)
                    if len(other_norm) < _NEAR_DUP_MIN_TITLE:
                        continue
                    if SequenceMatcher(None, norm, other_norm).ratio() >= _NEAR_DUP_RATIO:
                        duplicate_of = i
                        break
            if duplicate_of is None:
                kept.append(hit)
            elif tier_for(hit.url) < tier_for(kept[duplicate_of].url):
                kept[duplicate_of] = hit  # fica a versão de tier melhor
        return kept

    async def _triage(
        self, topic: str, hits: list[SearchHit], max_new: int
    ) -> list[SearchHit]:
        if max_new <= 0:
            return []
        candidates = self._dedup_candidates(hits)
        if not candidates:
            return []
        # Tier primeiro (em código, não por prompt): fonte primária ganha da genérica.
        candidates.sort(key=lambda h: tier_for(h.url))
        if len(candidates) <= max_new:
            return self._cap_per_domain(candidates, max_new)

        lines = []
        for i, hit in enumerate(candidates, start=1):
            date_part = f", {hit.published_at}" if hit.published_at else ""
            lines.append(
                f"{i}. [tier {tier_for(hit.url)}{date_part}] {hit.title}\n"
                f"   {hit.url}\n   {hit.snippet[:200]}"
            )
        system, user = triage_prompts(topic, "\n".join(lines), max_new)
        obj = await self._llm_json_soft(
            system, user, max_tokens=1500, step="triage", list_keys=("selected",)
        )
        if obj:
            picked_ns: list[int] = []
            for item in obj.get("selected") or []:
                if not isinstance(item, dict):
                    continue
                try:
                    picked_ns.append(int(item.get("n")))
                except (TypeError, ValueError):
                    continue
            picked = [candidates[n - 1] for n in picked_ns if 1 <= n <= len(candidates)]
            if picked:
                return self._cap_per_domain(picked, max_new)
        # Triage é otimização, não fundação: degrada para a ordem por tier.
        return self._cap_per_domain(candidates, max_new)

    def _cap_per_domain(self, hits: list[SearchHit], max_new: int) -> list[SearchHit]:
        seen: dict[str, int] = {}
        out: list[SearchHit] = []
        for hit in hits:
            domain = domain_of(hit.url)
            if seen.get(domain, 0) >= _MAX_PER_DOMAIN:
                continue
            seen[domain] = seen.get(domain, 0) + 1
            out.append(hit)
            if len(out) >= max_new:
                break
        return out

    async def _fetch_all(self, selected: list[SearchHit]) -> list[PageContent]:
        sem = asyncio.Semaphore(self.budget.fetch_concurrency)

        async def one(hit: SearchHit) -> PageContent | None:
            async with sem:
                max_chars = (
                    _FETCH_MAX_CHARS_TIER1
                    if tier_for(hit.url) == TIER_ACADEMIC
                    else _FETCH_MAX_CHARS
                )
                self._fetch_attempts += 1
                try:
                    result = await self.search.fetch(hit.url, max_chars=max_chars)
                except Exception as e:  # noqa: BLE001
                    logger.warning("[Research] fetch %s falhou: %s", hit.url, e)
                    return None
                self.stats.fetches += 1
                if isinstance(result, PageError):
                    await self.progress.log(f"Fonte pulada ({result.error}): {hit.url}")
                    return None
                if not result.title and hit.title:
                    result.title = hit.title
                if not result.published_at:
                    result.published_at = hit.published_at
                return result

        pages = await asyncio.gather(*(one(h) for h in selected))
        fresh: list[PageContent] = []
        for page in pages:
            if page is None or page.url in self._sources:
                continue
            self._sources[page.url] = Source(
                n=len(self._sources) + 1,
                title=page.title or page.url,
                url=page.url,
                published_at=_norm_date(page.published_at),
            )
            self._pages.append(page)
            fresh.append(page)
        return fresh

    async def _extract_evidence(
        self, topic: str, plan: ResearchPlan, pages: list[PageContent]
    ) -> list[Evidence]:
        sub_block = "\n".join(f"- {s.id}: {s.question}" for s in plan.sub_questions)
        batches = [
            pages[i : i + _EVIDENCE_BATCH_SIZE]
            for i in range(0, len(pages), _EVIDENCE_BATCH_SIZE)
        ]
        sem = asyncio.Semaphore(_EVIDENCE_CONCURRENCY)
        valid_sub_ids = {s.id for s in plan.sub_questions}

        async def one(batch: list[PageContent]) -> list[dict]:
            block = "\n\n".join(
                f'<web_content url="{p.url}">\n{p.text}\n</web_content>' for p in batch
            )
            system, user = evidence_prompts(topic, sub_block, block)
            async with sem:
                items = await self._llm_json_list(
                    system, user, key="evidence",
                    max_tokens=_EVIDENCE_MAX_TOKENS, step="evidence",
                )
            urls = {p.url for p in batch}
            out: list[dict] = []
            dropped = 0
            for item in items:
                if not item.get("claim"):
                    continue
                url = str(item.get("url") or "").strip()
                if url not in urls:
                    # O modelo às vezes devolve a URL levemente diferente
                    # (barra final, querystring). Casa por prefixo antes de
                    # descartar — evidência sem fonte confiável não pode ser
                    # citada, mas jogar fora por um "/" a mais é desperdício.
                    match = next(
                        (u for u in urls if u.rstrip("/") == url.rstrip("/")), None
                    ) or (batch[0].url if len(batch) == 1 else None)
                    if match is None:
                        dropped += 1
                        continue
                    url = match
                item["url"] = url
                out.append(item)
            if dropped:
                logger.warning(
                    "[Research] evidence: %d item(ns) descartado(s) por URL não reconhecida", dropped
                )
            return out

        results = await asyncio.gather(*(one(b) for b in batches))

        by_url = {p.url: p for p in pages}
        evidence: list[Evidence] = []
        for item in [i for batch in results for i in batch]:
            url = item["url"]
            page = by_url.get(url)
            sub_id = str(item.get("sub_question") or "") or None
            if sub_id not in valid_sub_ids:
                sub_id = None
            self._evidence_seq += 1
            evidence.append(
                Evidence(
                    id=f"e{self._evidence_seq}",
                    claim=str(item["claim"]).strip(),
                    quote=str(item.get("quote") or "").strip()[:400],
                    url=url,
                    published_at=_norm_date(page.published_at) if page else None,
                    source_type=source_type_for(url),  # do domínio, não do LLM
                    stance=(str(item["stance"]).strip() if item.get("stance") else None),
                    sub_question_id=sub_id,
                )
            )
        return evidence

    async def _reflect(self, topic: str, plan: ResearchPlan, today: str) -> list[dict]:
        matrix = coverage_matrix(plan, self._ledger)
        min_evidence = 1 if self.budget.max_rounds <= 1 else 2
        gaps = [
            row for row in matrix
            if row["id"] != "-" and (
                row["evidence"] < min_evidence or not row["has_strong_source"]
            )
        ]
        if not gaps:
            await self.progress.log("Cobertura completa — encerrando a coleta mais cedo")
            return []

        block = "\n".join(
            f"- {row['id']}: {row['question']} → {row['evidence']} evidência(s), "
            f"fontes: {', '.join(row['source_types']) or 'nenhuma'}"
            for row in matrix
        )
        block += "\n\nBuscas já feitas:\n" + "\n".join(
            f"- {key.split('|')[0]}" for key in sorted(self._searched)
        )
        max_new = max(2, self.budget.max_queries // 2)
        system, user = reflect_prompts(topic, block, max_new, today)
        obj = await self._llm_json_soft(
            system, user, max_tokens=1200, step="reflect",
            list_keys=("queries", "new_sub_questions"),
        )
        if not obj:
            return []

        existing = {s.id for s in plan.sub_questions}
        for item in obj.get("new_sub_questions") or []:
            if not isinstance(item, dict) or not item.get("question"):
                continue
            new_id = str(item.get("id") or f"q{len(plan.sub_questions) + 1}")
            if new_id in existing:
                continue
            plan.sub_questions.append(
                SubQuestion(id=new_id, question=str(item["question"]).strip())
            )
            existing.add(new_id)
            # A sub-pergunta nova precisa de dono: entra na seção mais próxima
            # (a primeira sem cobertura explícita, ou a última do outline).
            target = next((s for s in plan.outline if not s.covers), plan.outline[-1])
            target.covers.append(new_id)

        return self._parse_queries(obj.get("queries"), max_new)

    # ------------------------------------------------------------------
    # 3. ORGANIZE
    # ------------------------------------------------------------------

    def _ledger_for_organize(self) -> list[Evidence]:
        if len(self._ledger) <= _MAX_LEDGER_FOR_ORGANIZE:
            return self._ledger
        rank = {"academic": 0, "official": 1, "news": 2, "encyclopedia": 3, "other": 4}
        ordered = sorted(self._ledger, key=lambda e: rank.get(e.source_type, 5))
        return ordered[:_MAX_LEDGER_FOR_ORGANIZE]

    async def _organize(
        self, topic: str, plan: ResearchPlan, ledger: list[Evidence]
    ) -> dict[str, SectionCluster]:
        selected = self._ledger_for_organize()
        outline_block = "\n".join(
            f"- {s.section} (cobre: {', '.join(s.covers) or '—'})" for s in plan.body_sections()
        )
        evidence_block = "\n".join(
            f"{e.id} | {e.claim} | {e.source_type}"
            f"{f' | {e.published_at}' if e.published_at else ''}"
            f"{f' | posição: {e.stance}' if e.stance else ''}"
            for e in selected
        )
        system, user = organize_prompts(topic, outline_block, evidence_block)
        obj = await self._llm_json_soft(
            system, user, max_tokens=2500, step="organize", list_keys=("sections",)
        )

        by_id = {e.id: e for e in ledger}
        clusters: dict[str, SectionCluster] = {}
        if obj:
            for item in obj.get("sections") or []:
                if not isinstance(item, dict) or not item.get("section"):
                    continue
                name = str(item["section"]).strip()
                ids = [str(i) for i in (item.get("evidence_ids") or []) if str(i) in by_id]
                clusters[name] = SectionCluster(
                    section=name,
                    evidence_ids=ids,
                    convergences=[str(c) for c in (item.get("convergences") or [])],
                    divergences=[str(d) for d in (item.get("divergences") or [])],
                    links=[str(l) for l in (item.get("links") or [])],
                )

        # Fallback determinístico: seção sem cluster recebe a evidência das
        # sub-perguntas que ela declarou cobrir (nunca deixa seção sem material).
        for section in plan.body_sections():
            cluster = clusters.get(section.section)
            if cluster and cluster.evidence_ids:
                continue
            ids = [e.id for e in ledger if e.sub_question_id in set(section.covers)]
            if not ids:
                ids = [e.id for e in ledger[:8]]
            clusters[section.section] = SectionCluster(
                section=section.section,
                evidence_ids=ids,
                convergences=cluster.convergences if cluster else [],
                divergences=cluster.divergences if cluster else [],
                links=cluster.links if cluster else [],
            )
        return clusters

    # ------------------------------------------------------------------
    # 4. WRITE (modelo forte)
    # ------------------------------------------------------------------

    def _evidence_block(self, cluster: SectionCluster) -> str:
        by_id = {e.id: e for e in self._ledger}
        lines: list[str] = []
        for eid in cluster.evidence_ids:
            e = by_id.get(eid)
            if not e:
                continue
            source = self._sources.get(e.url)
            if not source:
                continue
            meta = [e.source_type]
            if e.published_at:
                meta.append(e.published_at)
            line = f"- {e.id} [{source.n}] ({', '.join(meta)}) {e.claim}"
            if e.quote:
                line += f'\n    trecho: "{e.quote}"'
            if e.stance:
                line += f"\n    posição do autor: {e.stance}"
            lines.append(line)
        return "\n".join(lines) or "(sem evidência atribuída a esta seção)"

    def _relations_block(self, cluster: SectionCluster) -> str:
        parts: list[str] = []
        if cluster.convergences:
            parts.append("Convergências:\n" + "\n".join(f"- {c}" for c in cluster.convergences))
        if cluster.divergences:
            parts.append("Divergências:\n" + "\n".join(f"- {d}" for d in cluster.divergences))
        if cluster.links:
            parts.append("Conexões com outras seções:\n" + "\n".join(f"- {l}" for l in cluster.links))
        return "\n\n".join(parts)

    def _outline_block(self, plan: ResearchPlan) -> str:
        return "\n".join(f"- {s.section}" for s in plan.body_sections())

    def _sources_with_evidence(self) -> int:
        """Fontes que RENDERAM evidência (≠ fontes citadas: o relatório pode
        deixar matéria-prima coletada na mesa)."""
        return len({e.url for e in self._ledger if e.url in self._sources})

    def _uncited_sources_with_evidence(self, markdown: str) -> list[Source]:
        """Fontes com evidência coletada que o relatório NÃO citou.

        É daqui que sai o conserto certo para "pediu 8 referências, citou 6":
        antes de gastar busca nova, usar o que já foi lido e extraído.
        """
        cited = cited_numbers(markdown)
        with_evidence = {e.url for e in self._ledger}
        return [
            s for url, s in self._sources.items()
            if url in with_evidence and s.n not in cited
        ]

    def _section_questions(self, plan: ResearchPlan, section: OutlineSection) -> str:
        by_id = {s.id: s.question for s in plan.sub_questions}
        lines = [f"- {by_id[qid]}" for qid in section.covers if qid in by_id]
        return "\n".join(lines) or "- (aprofunde o tema da seção)"

    async def _write_sections(
        self,
        topic: str,
        plan: ResearchPlan,
        clusters: dict[str, SectionCluster],
        instructions: str | None,
        min_sources: int | None = None,
        only: list[str] | None = None,
    ) -> dict[str, str]:
        sections = [
            s for s in plan.body_sections() if only is None or s.section in set(only)
        ]
        sem = asyncio.Semaphore(self.budget.write_concurrency)
        max_words = _words_for(self.budget.max_tokens_section)
        outline_block = self._outline_block(plan)
        goal = citation_goal_note(min_sources, self._sources_with_evidence())

        async def one(section: OutlineSection) -> tuple[str, str]:
            cluster = clusters.get(section.section) or SectionCluster(section=section.section)
            system, user = section_prompts(
                topic,
                plan.title,
                section.section,
                self._section_questions(plan, section),
                outline_block,
                self._evidence_block(cluster),
                self._relations_block(cluster),
                instructions,
                max_words,
                goal,
            )
            async with sem:
                try:
                    body = await self._llm_text(
                        system, user, max_tokens=self.budget.max_tokens_section
                    )
                except Exception as e:  # noqa: BLE001 — uma seção que falha não mata o relatório
                    logger.warning("[Research] seção '%s' falhou: %s", section.section, e)
                    return section.section, ""
            return section.section, body

        results = await asyncio.gather(*(one(s) for s in sections))
        return {name: body for name, body in results if body}

    async def _summary_and_conclusion(
        self,
        topic: str,
        plan: ResearchPlan,
        drafts: dict[str, str],
        instructions: str | None,
        requirements: RequirementSet,
        today: str,
    ) -> tuple[str, str]:
        drafts_block = "\n\n".join(
            f"## {s.section}\n{drafts.get(s.section, '(seção vazia)')[:4000]}"
            for s in plan.body_sections()
            if drafts.get(s.section)
        )
        system, user = summary_conclusion_prompts(
            topic,
            plan.title,
            drafts_block,
            instructions,
            self._requirements_block(requirements),
            today,
        )
        try:
            raw = await self._llm_text(
                system, user, max_tokens=self.budget.max_tokens_synthesis
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[Research] resumo/conclusão falharam: %s", e)
            return "", ""

        if _SUMMARY_MARKER in raw:
            rest = raw.split(_SUMMARY_MARKER, 1)[1]
            if _CONCLUSION_MARKER in rest:
                summary, conclusion = rest.split(_CONCLUSION_MARKER, 1)
                return summary.strip(), conclusion.strip()
            return rest.strip(), ""
        # Sem os marcadores: o texto vira resumo (melhor que descartar a chamada).
        return raw.strip(), ""

    # ------------------------------------------------------------------
    # Montagem
    # ------------------------------------------------------------------

    def _assemble(
        self,
        plan: ResearchPlan,
        drafts: dict[str, str],
        summary: str,
        conclusion: str,
        coverage_note: str = "",
    ) -> str:
        parts = [f"# {plan.title}", ""]
        if summary:
            parts += ["## Resumo executivo", "", summary, ""]
        for section in plan.body_sections():
            body = drafts.get(section.section)
            if not body:
                continue
            parts += [f"## {section.section}", "", body, ""]
        if conclusion:
            parts += ["## Conclusão", "", conclusion, ""]
        if coverage_note:
            parts += [coverage_note, ""]

        body_md = "\n".join(parts)
        sources = self._cited_sources(body_md)
        parts += ["## Fontes", ""]
        for source in sources:
            line = f"[{source.n}] {source.title} — {source.url}"
            if source.published_at:
                line += f" ({source.published_at})"
            parts.append(line)
        return "\n".join(parts).strip() + "\n"

    def _cited_sources(self, markdown: str) -> list[Source]:
        """Fontes CITADAS no corpo (a v1 listava tudo que baixou, citado ou não)."""
        cited = cited_numbers(markdown)
        sources = [s for s in self._sources.values() if s.n in cited]
        if not sources:
            # Relatório sem nenhuma citação: lista o que foi lido (melhor que
            # entregar seção de fontes vazia), mas isso já vira falha no VALIDATE.
            sources = list(self._sources.values())
        return sorted(sources, key=lambda s: s.n)

    def _requirements_block(self, requirements: RequirementSet) -> str:
        if not requirements.explicit:
            return ""
        return "\n".join(f"- {r.text}" for r in requirements.explicit)

    # ------------------------------------------------------------------
    # 5-6. VALIDATE + CRITIC + REVISE
    # ------------------------------------------------------------------

    async def _validate_and_revise(
        self,
        topic: str,
        plan: ResearchPlan,
        clusters: dict[str, SectionCluster],
        drafts: dict[str, str],
        summary: str,
        conclusion: str,
        report_md: str,
        requirements: RequirementSet,
        instructions: str | None,
        today: str,
    ) -> tuple[str, dict]:
        await self.progress.phase("validating", "Conferindo requisitos e cobertura")
        valid_ns = {s.n for s in self._sources.values()}
        validation = validate_report(
            report_md, plan, self._ledger, requirements, valid_ns,
            min_section_citations=self.budget.min_section_citations,
        )
        quality: dict = {
            "validation": validation.to_dict(),
            "coverage": coverage_matrix(plan, self._ledger),
            "rubric": {},
            "issues": [],
            "revised": False,
        }
        if self.budget.revision_cycles <= 0:
            return self._maybe_coverage_note(
                plan, drafts, summary, conclusion, report_md, validation, requirements
            ), quality

        rubric, issues = await self._critic(topic, report_md, requirements, validation)
        quality["rubric"] = rubric
        quality["issues"] = issues

        cycles = 0
        while cycles < self.budget.revision_cycles and (validation.failures or issues):
            cycles += 1
            await self.progress.phase(
                "revising", f"Revisão {cycles}: corrigindo {len(validation.failures) + len(issues)} ponto(s)"
            )

            # Faltam referências? Primeiro USE o que já foi lido: é comum sobrar
            # fonte com evidência extraída que o relatório simplesmente não citou
            # (30 evidências coletadas, 6 fontes citadas — §12.7). Buscar mais só
            # depois de esgotar a matéria-prima que já custou busca e leitura.
            uncited: list[Source] = []
            if "min_sources" in validation.failed_checks():
                uncited = self._uncited_sources_with_evidence(report_md)
                if uncited:
                    await self.progress.log(
                        f"{len(uncited)} fonte(s) já lidas e não citadas — "
                        "reescrevendo para aproveitá-las antes de buscar mais"
                    )
                elif self.budget.max_fetches - self._fetch_attempts > 0:
                    await self._extra_collection_round(topic, plan, today)
                    clusters = await self._organize(topic, plan, self._ledger)

            sections_to_fix = self._sections_to_fix(plan, validation, issues)
            if uncited and not sections_to_fix:
                # Nenhuma seção foi apontada, mas há fonte na mesa: reescreve as
                # seções que têm evidência dessas fontes.
                sections_to_fix = self._sections_with_sources(plan, clusters, uncited)
            if sections_to_fix:
                new_drafts = await self._revise_sections(
                    topic, plan, clusters, drafts, sections_to_fix, issues, validation,
                    uncited=uncited, min_sources=requirements.min_sources(),
                )
                drafts.update(new_drafts)
            else:
                # Nada acionável por seção (ex.: só min_sources) e sem mais coleta
                # possível → não adianta gastar outra rodada de escrita.
                if "min_sources" not in validation.failed_checks():
                    break

            summary, conclusion = await self._summary_and_conclusion(
                topic, plan, drafts, instructions, requirements, today
            )
            report_md = self._assemble(plan, drafts, summary, conclusion)
            self.stats.revisions = cycles
            quality["revised"] = True

            validation = validate_report(
                report_md, plan, self._ledger, requirements,
                {s.n for s in self._sources.values()},
                min_section_citations=self.budget.min_section_citations,
            )
            quality["validation"] = validation.to_dict()
            quality["coverage"] = coverage_matrix(plan, self._ledger)
            issues = []  # o critic só roda uma vez (custo); o validador segue mandando

        return self._maybe_coverage_note(
            plan, drafts, summary, conclusion, report_md, validation, requirements
        ), quality

    async def _critic(
        self,
        topic: str,
        report_md: str,
        requirements: RequirementSet,
        validation,
    ) -> tuple[dict, list[dict]]:
        validation_block = "\n".join(f"- {f.detail}" for f in validation.failures)
        system, user = critic_prompts(
            topic,
            report_md[:_CRITIC_REPORT_CHARS],
            self._requirements_block(requirements),
            validation_block,
        )
        obj = await self._llm_json_soft(
            system, user, max_tokens=_CRITIC_MAX_TOKENS, step="critic", strong=True,
            list_keys=("issues",),
        )
        if not obj:
            return {}, []
        scores = obj.get("scores") if isinstance(obj.get("scores"), dict) else {}
        rubric = {}
        for key in ("depth", "coherence", "coverage", "evidence"):
            try:
                rubric[key] = int(scores.get(key))
            except (TypeError, ValueError):
                continue
        issues = [
            {
                "section": str(i.get("section") or "").strip(),
                "issue": str(i.get("issue") or "").strip(),
                "fix": str(i.get("fix") or "").strip(),
            }
            for i in (obj.get("issues") or [])
            if isinstance(i, dict) and i.get("issue")
        ]
        return rubric, issues

    def _sections_to_fix(self, plan: ResearchPlan, validation, issues: list[dict]) -> list[str]:
        names = {s.section for s in plan.body_sections()}
        to_fix: set[str] = set()
        for failure in validation.failures:
            if failure.section and failure.section in names:
                to_fix.add(failure.section)
        for issue in issues:
            section = issue.get("section") or ""
            if section in names:
                to_fix.add(section)
        return [s.section for s in plan.body_sections() if s.section in to_fix]

    def _sections_with_sources(
        self, plan: ResearchPlan, clusters: dict[str, SectionCluster], sources: list[Source]
    ) -> list[str]:
        """Seções cujo cluster tem evidência das fontes dadas (as não citadas)."""
        wanted = {s.url for s in sources}
        by_id = {e.id: e for e in self._ledger}
        out: list[str] = []
        for section in plan.body_sections():
            cluster = clusters.get(section.section)
            if not cluster:
                continue
            if any(
                by_id[eid].url in wanted
                for eid in cluster.evidence_ids
                if eid in by_id
            ):
                out.append(section.section)
        return out

    async def _revise_sections(
        self,
        topic: str,
        plan: ResearchPlan,
        clusters: dict[str, SectionCluster],
        drafts: dict[str, str],
        sections: list[str],
        issues: list[dict],
        validation,
        uncited: list[Source] | None = None,
        min_sources: int | None = None,
    ) -> dict[str, str]:
        sem = asyncio.Semaphore(self.budget.write_concurrency)
        max_words = _words_for(self.budget.max_tokens_section)
        by_id = {e.id: e for e in self._ledger}
        uncited_urls = {s.url for s in (uncited or [])}

        async def one(name: str) -> tuple[str, str]:
            cluster = clusters.get(name) or SectionCluster(section=name)
            lines = [
                f"- {i['issue']} → conserto: {i['fix']}"
                for i in issues
                if i.get("section") == name
            ]
            lines += [
                f"- {f.detail}" for f in validation.failures if f.section == name
            ]
            # Fontes já lidas que esta seção tem em mãos e não citou.
            if uncited_urls:
                available = sorted(
                    {
                        self._sources[by_id[eid].url].n
                        for eid in cluster.evidence_ids
                        if eid in by_id and by_id[eid].url in uncited_urls
                        and by_id[eid].url in self._sources
                    }
                )
                if available:
                    goal = f" (o pedido exige {min_sources} referências)" if min_sources else ""
                    lines.append(
                        f"- Esta seção tem evidência das fontes {available} e NÃO as "
                        f"citou{goal}. Incorpore o que elas sustentam de verdade — "
                        "sem citação decorativa e sem inventar afirmação."
                    )
            system, user = revise_prompts(
                topic,
                name,
                drafts.get(name, ""),
                "\n".join(lines) or "- aprofunde a análise e sustente com citações",
                self._evidence_block(cluster),
                self._relations_block(cluster),
                max_words,
            )
            async with sem:
                try:
                    body = await self._llm_text(
                        system, user, max_tokens=self.budget.max_tokens_section
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("[Research] revisão de '%s' falhou: %s", name, e)
                    return name, ""
            return name, body

        results = await asyncio.gather(*(one(s) for s in sections))
        return {name: body for name, body in results if body}

    async def _extra_collection_round(
        self, topic: str, plan: ResearchPlan, today: str
    ) -> None:
        """Mini-rodada quando faltou FONTE (requisito de nº mínimo não cumprido)."""
        queries = await self._reflect(topic, plan, today)
        if not queries:
            # Cobertura "ok" mas faltam referências: repete o plano — o dedup por
            # URL garante que só entram fontes novas.
            queries = plan.queries
        await self.progress.phase("searching", "Buscando fontes adicionais (requisito)")
        hits = await self._search_all(queries)
        allowance = self.budget.max_fetches - self._fetch_attempts
        selected = await self._triage(topic, hits, allowance)
        if not selected:
            return
        await self.progress.phase("reading", f"Lendo mais {len(selected)} fonte(s)")
        pages = await self._fetch_all(selected)
        if not pages:
            return
        self._ledger.extend(await self._extract_evidence(topic, plan, pages))
        await self.progress.log(
            f"Mini-rodada: +{len(pages)} fonte(s) para cumprir o requisito de referências"
        )

    def _maybe_coverage_note(
        self,
        plan: ResearchPlan,
        drafts: dict[str, str],
        summary: str,
        conclusion: str,
        report_md: str,
        validation,
        requirements: RequirementSet,
    ) -> str:
        """Requisito não cumprido ou citação sem fonte → o relatório DIZ isso.

        Citação fantasma entra aqui porque é o pior defeito possível num relatório:
        o texto entregue não pode exibir um [n] fabricado sem avisar o leitor.
        """
        gaps = [
            f for f in validation.failures
            if f.check in ("min_sources", "section_required", "citation_unknown")
        ]
        if not gaps:
            return report_md
        note = "> **Nota de cobertura:** " + " ".join(f.detail for f in gaps)
        return self._assemble(plan, drafts, summary, conclusion, coverage_note=note)
