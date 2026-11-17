"""LLM write planner for PKS memory operations."""

from __future__ import annotations

import asyncio
import json
import logging
import os

from core.liveness import touch_progress
from core.llm.router import LLMRouter
from core.pks.schemas import PKSOperation
from shared import Message, MessageRole

logger = logging.getLogger(__name__)


# Teto de parede para a chamada do planner.
#
# O turno do desktop só sobrevive enquanto o keepalive do gRPC atesta vida:
# 240s de janela no core (``grpc_server._STALL_WINDOW_S``) + 90s de paciência do
# cliente (``ai_agent.rs`` ``STREAM_IDLE_TIMEOUT``) = **330s**. O SDK da OpenAI,
# sem ``timeout`` explícito, só desiste aos 600s — quase o DOBRO da janela. Em
# 2026-09-28 três chamadas a ``deepseek-v4-flash@baidu`` penduraram (o POST saiu,
# a resposta nunca voltou) e mataram turnos inteiros: 303s e 323s, com o usuário
# vendo "Neo Core ficou 90s sem dar sinal de progresso".
#
# Gravar memória é ACESSÓRIO: não pode derrubar a conversa. Aqui a gente desiste
# muito antes da janela e o turno segue vivo sem o que o planner ia gravar.
_PLANNER_TIMEOUT_S = float(os.getenv("NEO_PKS_PLANNER_TIMEOUT_S", "45"))


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
- Para relation_type "owns" em que o dono é o usuário atual/Daniel, use from_entity
  type "person" e name "Daniel Morais"; nunca use type "identity" para o usuário.
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
{confirmed_override}
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


# Override injetado quando a escrita foi acionada por uma AÇÃO EXPLÍCITA do
# usuário (tool pks_remember): quem decidiu "salvar" foi o cognitivo, que já
# entendeu o pedido e resolveu a referência ("essa ideia"). O planner NÃO
# re-julga se salva — só estrutura. Sem isso, o gate de "sugestão do assistente
# sem confirmação" poderia devolver {"operations": []} e anular a decisão do
# LLM logo acima dele (docs/pks-write-on-demand-plan.md §4.1).
_CONFIRMED_SAVE_OVERRIDE = """
MODO CONFIRMADO (prioridade máxima): o conteúdo abaixo JÁ foi confirmado pelo
usuário via ação explícita de "salvar/lembrar". Sua ÚNICA tarefa é ESTRUTURÁ-LO
na ontologia certa (entidade/campos/relações) — NÃO decida se vale a pena
salvar, NÃO devolva operações vazias por achar que é "sugestão do assistente".
Extraia o(s) fato(s) do conteúdo, escolha o entity type mais específico
(preference, food, supplement, routine, place, person...) e proponha as
operações. Use confidence >= 0.90. Só devolva {"operations": []} se o conteúdo
for genuinamente inestruturável (vazio ou sem nenhum fato).
"""


# Override para a tool pks_forget: o usuário pediu para ESQUECER/APAGAR algo da
# memória. Aqui o planner NÃO cria nem atualiza nada — só localiza no "Contexto
# PKS atual" a(s) entidade(s) que casam com o alvo e emite soft_delete_entity.
# docs/pks-write-on-demand-plan.md §Fase 5.
_FORGET_OVERRIDE = """
MODO ESQUECER (prioridade máxima): o usuário pediu para REMOVER algo da memória.
Sua ÚNICA tarefa é encontrar, no "Contexto PKS atual" acima, a(s) entidade(s)
que correspondem ao alvo descrito e emitir operações **soft_delete_entity** para
elas (com o name e type EXATOS como aparecem no contexto). Regras:
- Emita SOMENTE operações "soft_delete_entity". NUNCA upsert_entity/upsert_field/
  upsert_relation neste modo — não crie nem altere nada.
- Só apague o que casa CLARAMENTE com o alvo. Na dúvida entre duas entidades,
  escolha a que casa melhor pelo nome; se nada casa, devolva {"operations": []}.
- Se o alvo não existe no contexto, devolva {"operations": []} (não invente nome).
- Use confidence >= 0.90.
Formato de cada operação: {"op": "soft_delete_entity", "confidence": 0.95,
"reason": "usuário pediu para esquecer", "entity": {"name": "<exato>", "type": "<exato>"}}
"""


class PKSMemoryPlanner:
    """Produces proposed PKS write operations using an LLM."""

    def __init__(self, llm_router: LLMRouter, db: object | None = None) -> None:
        self.llm = llm_router
        # DB is optional so tests/callers without one still work — the model then
        # comes from settings/default. When present, the panel-selected model for
        # role "memory_planner" (modules.model_config) overrides it.
        self.db = db

    async def plan(
        self,
        *,
        user_message: str,
        assistant_response: str,
        pks_context: str,
        recent_history: str,
        max_operations: int = 40,
        confirmed_save: bool = False,
        forget: bool = False,
    ) -> list[PKSOperation]:
        logger.info(
            "[PKS][plan_memory] start user_chars=%d assistant_chars=%d context_chars=%d history_chars=%d max_operations=%d confirmed_save=%s forget=%s",
            len(user_message or ""),
            len(assistant_response or ""),
            len(pks_context or ""),
            len(recent_history or ""),
            max_operations,
            confirmed_save,
            forget,
        )
        history = f"Mensagens recentes:\n{recent_history}\n\n" if recent_history else ""
        prompt = _MEMORY_PLANNER_PROMPT.replace("{pks_context}", pks_context or "(vazio)")
        if forget:
            override = _FORGET_OVERRIDE
        elif confirmed_save:
            override = _CONFIRMED_SAVE_OVERRIDE
        else:
            override = ""
        prompt = prompt.replace("{confirmed_override}", override)
        user_content = (
            f"{history}"
            f"Mensagem atual do usuário:\n{user_message}\n\n"
            f"Resposta atual do assistente:\n{assistant_response}\n\n"
            f"Limite de operações: {max_operations}"
        )
        model = "(não resolvido)"
        try:
            # Modelo EXPLÍCITO: sem isso, o provider caía no DEFAULT_MODEL, que já
            # foi "deepseek-chat" (aposentado → 400 invalid_request) e fazia TODA
            # gravação do PKS falhar silenciosamente (session-77a77b00). O padrão
            # (settings.pks_planner_model) é o fallback; a escolha feita no
            # neo-panel (role "memory_planner") tem prioridade e vale sem restart.
            model = await self._resolve_model()
            # O provider SEGUE o modelo escolhido — antes era deepseek fixo, o que
            # quebraria se a UI apontasse pra um modelo de outro provider.
            provider = self.llm.get_provider_for_model(model)
            logger.info(
                "[PKS][plan_memory] provider_selected provider=%s model=%s",
                getattr(provider, "name", provider.__class__.__name__),
                model,
            )
            # Esta é a ÚNICA chamada LLM do PKS e roda DENTRO do turno, sem
            # gerar bolha nenhuma: para o keepalive do gRPC o turno parece
            # parado. Avisar que há trabalho legítimo em curso é o que impede o
            # desktop de matar a conversa enquanto a memória é planejada.
            touch_progress()
            response = await asyncio.wait_for(
                provider.complete(
                    messages=[
                        Message(role=MessageRole.SYSTEM, content=prompt),
                        Message(role=MessageRole.USER, content=user_content),
                    ],
                    model=model,
                    temperature=0.0,
                    # Folga: o modelo às vezes monta um grafo maior (pessoa +
                    # coleção + entidade + relações) e, com o teto antigo (3000), o
                    # JSON truncava no meio → parse falhava → 0 operações salvas
                    # (session-77a77b00). O parser ainda salva o que completou.
                    max_tokens=8000,
                ),
                timeout=_PLANNER_TIMEOUT_S,
            )
            touch_progress()
            raw = response.get_content() or "{}"
            logger.info("[PKS][plan_memory] llm_response_received chars=%d", len(raw))
            logger.debug("[PKS][plan_memory] llm_response_preview=%s", raw[:500])
        except asyncio.TimeoutError:
            # Não é "falhou": é "demorou demais para algo acessório". Log próprio
            # porque cair no genérico esconde a causa (foi o que custou o
            # diagnóstico de 2026-09-28).
            logger.warning(
                "[PKS] Plan Memory desistiu após %.0fs (modelo=%s) — turno preservado, nada gravado",
                _PLANNER_TIMEOUT_S,
                model,
            )
            return []
        except Exception as exc:
            logger.warning("[PKS] Plan Memory LLM call failed: %s", exc)
            return []
        parsed = self._parse(raw, max_operations=max_operations)
        logger.info("[PKS][plan_memory] parse_complete operations=%d", len(parsed))
        return parsed

    async def _resolve_model(self) -> str:
        """Model for the planner: panel selection (DB) → settings → hard default.

        Reads role "memory_planner" from :mod:`modules.model_config` (memoized,
        never raises). Falls back to ``settings.pks_planner_model`` when nothing is
        persisted or the DB is absent, keeping the pre-panel behavior intact.
        """
        from shared.config import get_settings

        fallback = getattr(get_settings(), "pks_planner_model", "deepseek-flash")
        try:
            from modules.model_config import get_model_config

            config = await get_model_config(
                self.db, "memory_planner", fallback_model=fallback
            )
            return config.get("model") or fallback
        except Exception as e:  # pragma: no cover - defensive; never break writes
            logger.warning("[PKS] resolve planner model failed, using %s: %s", fallback, e)
            return fallback

    def _parse(self, raw: str, *, max_operations: int) -> list[PKSOperation]:
        logger.info("[PKS][plan_memory] parse_start raw_chars=%d", len(raw or ""))
        text = raw.strip()
        if "```" in text:
            start = text.find("{")
            end = text.rfind("}") + 1
            text = text[start:end] if start >= 0 and end > start else text
        try:
            data = json.loads(text)
            items = data.get("operations", [])
        except Exception:
            # JSON truncado (ex.: modelo estourou max_tokens no meio do array):
            # salva as operações que COMPLETARAM em vez de perder tudo.
            items = self._salvage_operations(text)
            if items:
                logger.warning(
                    "[PKS] Plan Memory JSON truncado — salvando %d op(s) completas",
                    len(items),
                )
            else:
                logger.warning("[PKS] Could not parse Plan Memory JSON: %s", raw[:300])
                return []

        parsed: list[PKSOperation] = []
        skipped = 0
        for item in items:
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

    @staticmethod
    def _salvage_operations(text: str) -> list[dict]:
        """Extrai objetos de operação completos de um JSON truncado.

        Varre o array ``"operations": [ ... ]`` casando chaves ``{}`` (ignorando
        as que estão dentro de strings) e decodifica cada objeto de topo do
        array isoladamente. O último objeto, se veio cortado, é descartado — o
        que já completou é preservado. Melhor salvar N-1 fatos do que zero.
        """
        marker = text.find('"operations"')
        if marker < 0:
            return []
        start = text.find("[", marker)
        if start < 0:
            return []
        items: list[dict] = []
        depth = 0
        obj_start = -1
        in_str = False
        escape = False
        for i in range(start + 1, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                if depth == 0:
                    obj_start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and obj_start >= 0:
                    chunk = text[obj_start : i + 1]
                    try:
                        items.append(json.loads(chunk))
                    except Exception:
                        pass
                    obj_start = -1
            elif ch == "]" and depth == 0:
                break
        return items
