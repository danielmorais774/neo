"""LLM write planner for PKS memory operations."""

from __future__ import annotations

import json
import logging

from core.llm.router import LLMRouter
from core.pks.schemas import PKSOperation
from shared import Message, MessageRole

logger = logging.getLogger(__name__)


_MEMORY_PLANNER_PROMPT = """\
Você é o Plan Memory do Personal Knowledge Store (PKS).

Sua tarefa é transformar a mensagem atual do usuário em um plano de escrita
estruturado. Você NÃO escreve no banco. Você apenas propõe operações; o backend
vai validar ontologia, resolver entidades e executar.

Salve apenas fatos pessoais, duráveis e úteis que o usuário afirmou ou confirmou.
Não salve fatos gerais, perguntas, suposições, nem sugestões do assistente sem
confirmação explícita do usuário.

Tipos de entidade válidos:
identity, person, project, company, device, technology, document, workout, exercise,
muscle_group, meal, food, supplement, health_state, restriction, routine, goal,
preference, vehicle, property, account, pet, place, software, hardware_problem

Tipos de relação válidos:
owns, works_on, uses_stack, developed_on, used_for, has_exercise, targets,
uses_equipment, sibling_of, child_of, parent_of, belongs_to, related_to, mentions,
located_in, has_member, performed

Operações válidas:
upsert_entity, upsert_field, upsert_relation, expire_relation, soft_delete_entity

Regras obrigatórias:
- Responda somente JSON válido.
- Use confidence >= 0.80 apenas quando o usuário afirmou claramente.
- Use sensitivity "private" por padrão; "sensitive" para saúde, finanças ou dados íntimos.
- Para upsert_field, sempre informe entity.name e entity.type.
- Para upsert_relation, sempre informe from_entity, relation_type e to_entity.
- Resolva por entidade canônica. Não crie entity genérica se já houver contexto PKS.
- Se a mensagem atual não tem fato persistente, retorne {"operations": []}.
- Foque na mensagem atual. Use histórico só para resolver pronomes e contexto.
- Exceção: se a mensagem atual for uma confirmação explícita para salvar/lembrar algo
  ("salva na memória", "guarda isso", "lembra disso"), você pode persistir o
  conteúdo estruturado da resposta anterior do assistente que o usuário acabou de confirmar.

Regras de domínio para academia:
- Plano semanal de academia é entity type "routine", preferencialmente "Plano de treino de academia".
- Treinos por dia são type "workout": "Treino de segunda-feira", "Treino de terça-feira" etc.
- Grupos musculares são type "muscle_group": Peito, Tríceps, Costas, Bíceps, Perna, Ombro.
- Exercícios são type "exercise".
- Vincule rotina -> treinos com relation_type "has_member".
- Vincule treino -> grupos musculares com relation_type "targets".
- Vincule treino -> exercícios com relation_type "has_exercise".
- Campos úteis:
  - routine_type = "split"
  - frequency_per_week = número
  - weekday = "monday" | "tuesday" | "wednesday" | "thursday" | "friday" | "saturday" | "sunday"
  - label = texto do dia
  - sets = número de séries/sessões
  - reps = número de repetições
  - weight_kg = carga em quilos
  - weight_note = observação quando a carga não foi informada
- Não salve treino semanal como blob único se for possível salvar dias, grupos e exercícios.

Contexto PKS atual:
{pks_context}

Formato:
{{
  "operations": [
    {{
      "op": "upsert_entity",
      "confidence": 0.95,
      "reason": "usuário informou uma rotina de treino",
      "entity": {{
        "name": "Plano de treino de academia",
        "type": "routine",
        "aliases": ["plano de treino", "treino split"],
        "space": "Academia",
        "collection": "Rotinas",
        "metadata": {{}},
        "sensitivity": "private"
      }}
    }},
    {{
      "op": "upsert_field",
      "confidence": 0.95,
      "reason": "usuário informou a carga do exercício",
      "field": {{
        "entity": {{"name": "Supino", "type": "exercise"}},
        "key": "weight_kg",
        "value_type": "number",
        "value": 40,
        "unit": "kg",
        "sensitivity": "private"
      }}
    }},
    {{
      "op": "upsert_relation",
      "confidence": 0.95,
      "reason": "usuário informou que o treino de segunda contém supino",
      "relation": {{
        "from_entity": {{"name": "Treino de segunda-feira", "type": "workout"}},
        "relation_type": "has_exercise",
        "to_entity": {{"name": "Supino", "type": "exercise"}},
        "sensitivity": "private"
      }}
    }}
  ]
}}
"""


class PKSMemoryPlanner:
    """Produces proposed PKS write operations using an LLM."""

    def __init__(self, llm_router: LLMRouter) -> None:
        self.llm = llm_router

    async def plan(
        self,
        *,
        user_message: str,
        assistant_response: str,
        pks_context: str,
        recent_history: str,
        max_operations: int = 40,
    ) -> list[PKSOperation]:
        logger.info(
            "[PKS][plan_memory] start user_chars=%d assistant_chars=%d context_chars=%d history_chars=%d max_operations=%d",
            len(user_message or ""),
            len(assistant_response or ""),
            len(pks_context or ""),
            len(recent_history or ""),
            max_operations,
        )
        history = f"Mensagens recentes:\n{recent_history}\n\n" if recent_history else ""
        prompt = _MEMORY_PLANNER_PROMPT.replace("{pks_context}", pks_context or "(vazio)")
        user_content = (
            f"{history}"
            f"Mensagem atual do usuário:\n{user_message}\n\n"
            f"Resposta atual do assistente:\n{assistant_response}\n\n"
            f"Limite de operações: {max_operations}"
        )
        try:
            provider = self._provider()
            logger.info(
                "[PKS][plan_memory] provider_selected provider=%s",
                getattr(provider, "name", provider.__class__.__name__),
            )
            response = await provider.complete(
                messages=[
                    Message(role=MessageRole.SYSTEM, content=prompt),
                    Message(role=MessageRole.USER, content=user_content),
                ],
                temperature=0.0,
                max_tokens=3000,
            )
            raw = response.get_content() or "{}"
            logger.info("[PKS][plan_memory] llm_response_received chars=%d", len(raw))
            logger.debug("[PKS][plan_memory] llm_response_preview=%s", raw[:500])
        except Exception as exc:
            logger.warning("[PKS] Plan Memory LLM call failed: %s", exc)
            return []
        parsed = self._parse(raw, max_operations=max_operations)
        logger.info("[PKS][plan_memory] parse_complete operations=%d", len(parsed))
        return parsed

    def _provider(self):
        for provider_name in ("deepseek", None):
            try:
                return self.llm.get_provider(provider_name)  # type: ignore[arg-type]
            except Exception:
                continue
        raise RuntimeError("No LLM provider available for PKS memory planner")

    def _parse(self, raw: str, *, max_operations: int) -> list[PKSOperation]:
        logger.info("[PKS][plan_memory] parse_start raw_chars=%d", len(raw or ""))
        text = raw.strip()
        if "```" in text:
            start = text.find("{")
            end = text.rfind("}") + 1
            text = text[start:end] if start >= 0 and end > start else text
        try:
            data = json.loads(text)
        except Exception:
            logger.warning("[PKS] Could not parse Plan Memory JSON: %s", raw[:300])
            return []

        parsed: list[PKSOperation] = []
        skipped = 0
        for item in data.get("operations", []):
            if not isinstance(item, dict):
                skipped += 1
                continue
            try:
                parsed.append(PKSOperation.model_validate(item))
            except Exception:
                skipped += 1
                logger.warning("[PKS] Invalid Plan Memory operation skipped: %s", item)
        limited = parsed[:max_operations]
        logger.info(
            "[PKS][plan_memory] parse_result valid=%d skipped=%d limited=%d",
            len(parsed),
            skipped,
            len(limited),
        )
        return limited
