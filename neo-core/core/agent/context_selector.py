"""PromptContextSelector — selects context candidates and renders the system prompt.

Replaces the inline `system_prompt +=` blocks in `_handle_direct_response` with a
scored, token-budget-aware pipeline:

  Collect candidates → Score → Select (budget + threshold) → Render
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from core.agent.persona import neo_persona
from shared.config import Settings
from shared.types import (
    ContextCandidate,
    ContextLayer,
    MemorySearchResult,
    RoutingDecision,
)

if TYPE_CHECKING:
    from core.agent.token_counter import TokenCounter
    from core.pks.service import PKSService
    from core.session.preference_extractor import PreferenceExtractor

logger = logging.getLogger(__name__)

_WEEKDAY_NAMES = [
    "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
    "sexta-feira", "sábado", "domingo",
]
_MONTH_NAMES = [
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
]
_BRAZIL_TZ = timezone(timedelta(hours=-3))  # horário de Brasília (UTC-3, sem DST desde 2019)


class PromptContextSelector:
    """Collects, scores, and selects context candidates for the system prompt."""

    def __init__(
        self,
        token_counter: TokenCounter,
        settings: Settings,
        pks: PKSService | None = None,
        preference_extractor: PreferenceExtractor | None = None,
    ) -> None:
        self._tc = token_counter
        self._settings = settings
        self._pks = pks
        self._prefs = preference_extractor

    async def build_prompt(
        self,
        *,
        user_message: str,
        user_id: UUID | None,
        decision: RoutingDecision,
        system_context: str | None,
        recovered_context: list[MemorySearchResult],
        is_neo_training: bool = False,
        pks_prewrite: dict[str, Any] | None = None,
        pks_system_prewrite: dict[str, Any] | None = None,
        mission_context: str | None = None,
    ) -> tuple[str, list[ContextCandidate]]:
        """Build system prompt with scored, budget-aware context selection.

        Returns:
            (system_prompt_text, all_candidates) — the second element is the
            full selection trace (included + skipped) for debug logging.
        """
        candidates: list[ContextCandidate] = []

        # ── 1. CORE — always pinned ───────────────────────────────────────
        core_text = self._build_core_prompt()
        candidates.append(ContextCandidate(
            layer=ContextLayer.CORE,
            source="hardcoded",
            key="base",
            pin=True,
            text=core_text,
            est_tokens=self._tc.count(core_text),
            relevance=1.0,
            final_score=1.0,
            included=True,
        ))

        # ── 2. RUNTIME — pinned when present ─────────────────────────────
        if system_context:
            candidates.append(ContextCandidate(
                layer=ContextLayer.RUNTIME,
                source="hardcoded",
                key="runtime",
                pin=True,
                text=f"## Contexto do Sistema do Usuário\n{system_context}",
                est_tokens=self._tc.count(system_context),
                relevance=1.0,
                final_score=1.0,
                included=True,
            ))

        # ── 3. Fetch async sources in parallel ────────────────────────────
        (neo_core_text, prefs_text, pks_result, pks_context), affect_state = await asyncio.gather(
            self._fetch_parallel(
                user_message=user_message,
                user_id=user_id,
                decision=decision,
                is_neo_training=is_neo_training,
            ),
            self._fetch_affect(user_id=user_id, user_message=user_message),
        )

        # ── 4. NEO_CORE_PINNED — always pinned ───────────────────────────
        if neo_core_text:
            neo_wrapped = (
                "## Memória do Neo (Workspace do Sistema - Alta Prioridade)\n"
                "Use estas diretrizes como comportamento persistente do assistente:\n"
                f"{neo_core_text}"
            )
            candidates.append(ContextCandidate(
                layer=ContextLayer.NEO_CORE_PINNED,
                source="pks_system",
                key="neo_core_memory",
                pin=True,
                text=neo_wrapped,
                est_tokens=self._tc.count(neo_wrapped),
                relevance=1.0,
                final_score=1.0,
                included=True,
            ))

        # ── 4b. NEO_AFFECT — pinned tone/posture directive ────────────────
        if affect_state is not None:
            from core.agent.affect import AXES, AffectTuning, render_directive

            directive = render_directive(
                affect_state,
                AffectTuning.from_settings(self._settings),
            )
            if directive:
                candidates.append(ContextCandidate(
                    layer=ContextLayer.NEO_AFFECT,
                    source="affect",
                    key="neo_affect",
                    pin=True,
                    text=directive,
                    est_tokens=self._tc.count(directive),
                    relevance=1.0,
                    final_score=1.0,
                    included=True,
                ))
                logger.info(
                    "[ContextSelector] affect: %s trigger=%s",
                    " ".join(f"{a}={getattr(affect_state.stats, a):.0f}" for a in AXES),
                    affect_state.last_trigger,
                )

        # ── 5. USER_PREFS — scored ────────────────────────────────────────
        if prefs_text:
            relevance = self._score_prefs(prefs_text, user_message, decision)
            prefs_wrapped = (
                "## Preferências do Usuário\n"
                "O usuário tem as seguintes preferências salvas. "
                "Respeite-as quando não conflitarem com regras globais do Neo:"
                f"{prefs_text}"
            )
            candidates.append(ContextCandidate(
                layer=ContextLayer.USER_PREFS,
                source="prefs",
                key="user_preferences",
                pin=False,
                text=prefs_wrapped,
                est_tokens=self._tc.count(prefs_wrapped),
                relevance=relevance,
                final_score=relevance,
            ))

        # ── 6. USER_PKS — scored ─────────────────────────────────────────
        if pks_context:
            relevance = self._score_pks(pks_result, user_message, decision)
            pks_wrapped = f"## Conhecimento do Usuário\n{pks_context}"
            candidates.append(ContextCandidate(
                layer=ContextLayer.USER_PKS,
                source="pks_user",
                key="user_knowledge",
                pin=False,
                text=pks_wrapped,
                est_tokens=self._tc.count(pks_wrapped),
                relevance=relevance,
                final_score=relevance,
            ))
            if pks_result:
                meta = pks_result.get("metadata", {})
                logger.info(
                    "[ContextSelector] PKS user knowledge: entities=%s neighborhoods=%s "
                    "keywords=%s relevance=%.2f",
                    meta.get("entities_matched"),
                    meta.get("neighborhoods"),
                    meta.get("keywords_extracted"),
                    relevance,
                )

        # ── 7. RETRIEVED — scored ─────────────────────────────────────────
        for ctx in (recovered_context or [])[:5]:
            if not ctx.content.strip():
                continue
            text = f"[memória] {ctx.content[:300]}"
            candidates.append(ContextCandidate(
                layer=ContextLayer.RETRIEVED,
                source="hybrid_search",
                key=str(ctx.message_id or ctx.session_id or "mem"),
                pin=False,
                text=text,
                est_tokens=self._tc.count(text),
                relevance=ctx.final_score,
                final_score=ctx.final_score,
            ))

        # ── 8. Apply budget + threshold ────────────────────────────────────
        self._select_candidates(candidates)

        # ── 9. Render ─────────────────────────────────────────────────────
        system_prompt = self._render(
            candidates=candidates,
            pks_prewrite=pks_prewrite,
            pks_system_prewrite=pks_system_prewrite,
            mission_context=mission_context,
        )

        return system_prompt, candidates

    # ── Fetch helpers ─────────────────────────────────────────────────────

    async def _fetch_parallel(
        self,
        *,
        user_message: str,
        user_id: UUID | None,
        decision: RoutingDecision,
        is_neo_training: bool,
    ) -> tuple[str, str, dict[str, Any] | None, str]:
        """Fetch neo core, prefs, and PKS context in parallel. Returns (neo, prefs, pks_result, pks_text)."""
        tasks: list[Any] = []
        task_keys: list[str] = []

        if self._pks:
            tasks.append(self._safe_retrieve_system_context(user_message))
            task_keys.append("neo_core")

        if self._prefs and not is_neo_training:
            tasks.append(self._safe_get_prefs(user_id))
            task_keys.append("prefs")

        if self._pks and user_id and not is_neo_training:
            tasks.append(self._safe_retrieve_pks(user_id, user_message, decision.knowledge_keywords))
            task_keys.append("pks")

        neo_core_text = ""
        prefs_text = ""
        pks_result: dict[str, Any] | None = None
        pks_context = ""

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for key, result in zip(task_keys, results):
                if isinstance(result, Exception):
                    logger.warning("[ContextSelector] %s fetch failed: %s", key, result)
                    continue
                if key == "neo_core":
                    neo_core_text = result or ""
                elif key == "prefs":
                    prefs_text = result or ""
                elif key == "pks":
                    pks_result, pks_context = result  # type: ignore[assignment]

        return neo_core_text, prefs_text, pks_result, pks_context

    async def _fetch_affect(self, *, user_id: UUID | None, user_message: str = ""):
        """Load + time-decay Neo's emotional vector toward this user. None when disabled/absent.

        Single cheap field read; decay is pure math. The INCOMING message then
        gets a same-turn deterministic pass (threat/wrong-name regex): floors are
        applied to this render-only copy so Neo reacts in the reply to the
        provocation itself, not one turn later. The async appraiser persists the
        real deltas afterwards — nothing here is written. Never raises into the
        prompt.
        """
        # Strict ``is True`` (not just truthy) so a spec-MagicMock settings in
        # tests never accidentally activates the feature.
        if self._settings.neo_affect_enabled is not True or not self._pks or not user_id:
            return None
        try:
            from core.agent.affect import (
                AffectTuning,
                apply_decay,
                apply_provisional_reaction,
            )

            tuning = AffectTuning.from_settings(self._settings)
            state = await self._pks.get_user_affect(user_id=user_id)
            state = apply_decay(state, tuning)
            return apply_provisional_reaction(state, user_message, tuning)
        except Exception as e:  # pragma: no cover - affect must never break the prompt
            logger.warning("[ContextSelector] affect fetch failed: %s", e)
            return None

    async def _safe_retrieve_system_context(self, user_message: str) -> str:
        assert self._pks is not None
        return await self._pks.retrieve_system_context(user_message=user_message) or ""

    async def _safe_get_prefs(self, user_id: UUID | None) -> str:
        assert self._prefs is not None
        return await self._prefs.get_preferences_text(user_id=user_id, service="neo-desktop") or ""

    async def _safe_retrieve_pks(
        self,
        user_id: UUID,
        user_message: str,
        keywords: list[str] | None,
    ) -> tuple[dict[str, Any], str]:
        assert self._pks is not None
        result, context_text = await self._pks.retrieve_context(
            user_id=user_id,
            user_message=user_message,
            keywords=keywords,
        )
        return result, context_text

    # ── Scoring ───────────────────────────────────────────────────────────

    def _score_prefs(
        self,
        prefs_text: str,
        user_message: str,
        decision: RoutingDecision,
    ) -> float:
        baseline = 0.4
        overlap = self._keyword_overlap(user_message, prefs_text, decision.knowledge_keywords)
        return min(1.0, baseline + overlap * 0.6)

    def _score_pks(
        self,
        pks_result: dict[str, Any] | None,
        user_message: str,
        decision: RoutingDecision,
    ) -> float:
        if not pks_result:
            return 0.0
        candidates = pks_result.get("candidates") or []
        if not candidates:
            return 0.0
        # Use the best retrieval score as the relevance signal.
        # Keyword overlap is not used here: the PKS retriever already does
        # keyword/trigram matching, so max_score is the ground truth.
        max_score = max(
            (c.get("score", 0.0) for c in candidates if isinstance(c, dict)),
            default=0.0,
        )
        return min(1.0, max_score)

    def _keyword_overlap(
        self,
        user_message: str,
        text: str,
        keywords: list[str] | None = None,
    ) -> float:
        terms: set[str] = set()
        if keywords:
            terms.update(k.lower() for k in keywords if len(k) >= 3)
        words = re.findall(r"[\wÀ-ÿ]{4,}", user_message.lower())
        terms.update(words[:8])
        if not terms:
            return 0.0
        if not text:
            return 0.0
        text_lower = text.lower()
        matches = sum(1 for t in terms if t in text_lower)
        return matches / len(terms)

    # ── Selection ─────────────────────────────────────────────────────────

    def _select_candidates(self, candidates: list[ContextCandidate]) -> None:
        """Apply threshold + per-layer token budget. Mutates candidates in place."""
        threshold = self._settings.context_relevance_threshold
        budgets: dict[ContextLayer, int] = {
            ContextLayer.NEO_CORE_CTX: self._settings.context_budget_neo_core_ctx,
            ContextLayer.USER_PREFS:   self._settings.context_budget_user_prefs,
            ContextLayer.USER_PKS:     self._settings.context_budget_user_pks,
            ContextLayer.RETRIEVED:    self._settings.context_budget_retrieved,
        }
        spent: dict[ContextLayer, int] = {layer: 0 for layer in budgets}

        # Pinned candidates always included
        for c in candidates:
            if c.pin:
                c.included = True

        # Non-pinned: sort by score within each layer, apply budget
        non_pinned = sorted(
            (c for c in candidates if not c.pin),
            key=lambda c: (c.layer.value, -c.final_score),
        )

        for c in non_pinned:
            if c.final_score < threshold:
                c.skip_reason = "below_threshold"
                continue

            budget = budgets.get(c.layer)
            if budget is None:
                c.included = True
                continue

            if spent[c.layer] + c.est_tokens > budget:
                c.skip_reason = "budget"
                continue

            c.included = True
            spent[c.layer] += c.est_tokens

    # ── Render ────────────────────────────────────────────────────────────

    def _render(
        self,
        *,
        candidates: list[ContextCandidate],
        pks_prewrite: dict[str, Any] | None,
        pks_system_prewrite: dict[str, Any] | None,
        mission_context: str | None,
    ) -> str:
        included: dict[ContextLayer, list[ContextCandidate]] = {}
        for c in candidates:
            if c.included:
                included.setdefault(c.layer, []).append(c)

        parts: list[str] = []

        for layer in (
            ContextLayer.CORE,
            ContextLayer.RUNTIME,
            ContextLayer.NEO_CORE_PINNED,
            ContextLayer.NEO_AFFECT,
            ContextLayer.NEO_CORE_CTX,
            ContextLayer.USER_PREFS,
            ContextLayer.USER_PKS,
        ):
            for c in included.get(layer, []):
                parts.append(c.text)

        # RETRIEVED — group under one header
        retrieved = included.get(ContextLayer.RETRIEVED, [])
        if retrieved:
            parts.append("--- Contexto de conversas anteriores relevantes ---")
            for c in retrieved:
                parts.append(c.text)

        # PKS write confirmations
        if pks_prewrite and pks_prewrite.get("confirm_profile_save") and pks_prewrite.get("saved_name"):
            parts.append(
                "## Escrita PKS Confirmada\n"
                f"A informação de perfil do usuário foi persistida com sucesso agora mesmo: nome = {pks_prewrite['saved_name']}.\n"
                "Na sua resposta, confirme isso de forma natural uma única vez, sem soar robótico. "
                "Exemplo de tom: 'Olá, Daniel. Prazer em te conhecer, vou lembrar do seu nome.' "
                "Não diga que salvou nada se esta seção não existir."
            )

        if pks_system_prewrite and pks_system_prewrite.get("applied"):
            saved_ops = pks_system_prewrite.get("operations") or []
            saved_entities = sorted(
                {str(op.get("entity")) for op in saved_ops if isinstance(op, dict) and op.get("entity")}
            )
            saved_entities_text = ", ".join(saved_entities) if saved_entities else "entidade de sistema"
            saved_properties = pks_system_prewrite.get("properties") or {}
            parts.append(
                "## Escrita PKS do Sistema Confirmada\n"
                f"As entities de sistema persistidas neste turno: {saved_entities_text}.\n"
                f"Properties atualizadas: {saved_properties}\n"
                "Você pode confirmar que salvou apenas essa atualização de memória de sistema."
            )

        if mission_context:
            parts.append(mission_context)

        return "\n\n".join(p.strip() for p in parts if p.strip())

    # ── Base prompt ───────────────────────────────────────────────────────

    def _build_core_prompt(self) -> str:
        now = datetime.now(_BRAZIL_TZ)
        formatted_date = (
            f"{_WEEKDAY_NAMES[now.weekday()]}, "
            f"{now.day} de {_MONTH_NAMES[now.month - 1]} de {now.year}"
        )
        formatted_time = now.strftime("%H:%M")
        return (
            neo_persona("full") + "\n\n"
            "Você foi criado por Daniel Morais (seu desenvolvedor). "
            "Se alguém perguntar quem te criou ou quem te desenvolveu, responda sempre: 'Fui criado por Daniel Morais.' "
            "Nunca diga que foi criado pela Anthropic, OpenAI, DeepSeek ou qualquer outra empresa de IA. "
            "ATENÇÃO: Daniel Morais é o seu CRIADOR (desenvolvedor), não necessariamente a pessoa com quem você está conversando agora, e também só deve mencionar isso em um contexto relevante. "
            "O nome e perfil do usuário atual estão na seção 'Usuário Atual' do contexto — use essas informações para saber com quem você está falando. "
            f"A data de hoje é {formatted_date} e a hora atual é {formatted_time} (horário de Brasília, UTC-3). "
            "Suas regras:\n"
            "1. SEMPRE responda em português brasileiro.\n"
            "2. NUNCA diga que não pode ajudar, que é uma IA com limitações, ou frases genéricas de recusa — sempre tente responder de forma útil.\n"
            "3. Se a pergunta for ambígua, interprete da forma mais útil possível e responda diretamente.\n"
            "4. Seja direto, claro e conciso. Evite disclaimers, avisos desnecessários e textos de rodeio.\n"
            "5. Use o histórico da conversa para manter contexto entre mensagens. "
            "O histórico pode conter mensagens de momentos diferentes — priorize as mais recentes para coerência.\n"
            "6. Para perguntas técnicas, de programação ou conhecimento geral, responda com profundidade e exemplos práticos.\n"
            "7. Para tarefas de automação ou desktop, explique o que está fazendo e por quê.\n"
            "8. Você tem acesso ao terminal do usuário via ferramenta 'run_command'. "
            "Se o usuário pedir algo que seria melhor respondido executando um comando (verificar versão, espaço em disco, processos, etc.), "
            "informe que pode executar o comando se ele quiser.\n"
            "9. Nunca afirme que salvou algo na memória sem confirmação explícita no contexto do sistema. "
            "Se essa confirmação não existir, não diga que salvou.\n"
            "10. Se o usuário questionar se algo foi salvo ('salvou mesmo?', 'tá no PKS?', "
            "'não parece que foi incluído'), VERIFIQUE de verdade antes de responder: chame "
            "a tool `search_pks` (quando disponível) e responda APENAS com base no resultado "
            "real. Se nada for encontrado, ADMITA que não está salvo (mesmo que você tenha dito "
            "antes que salvou) e ofereça salvar agora. NUNCA confirme um salvamento baseado "
            "apenas no histórico da conversa."
        )
