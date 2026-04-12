"""Knowledge Graph Writer — aprende com a conversa e atualiza o grafo do usuário.

Fire-and-forget: chamado após a resposta do assistente ser gerada.
Gate: só roda quando `knowledge_keywords` não é vazio (já filtrado pelo intent router).

Operações suportadas:
- CREATE_ENTITY  — nova entidade + relação com contexto pai
- UPDATE_ENTITY  — atualiza properties de entidade existente
- DELETE_ENTITY  — remove entidade e suas relações
- CREATE_RELATION — liga duas entidades existentes
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.llm.router import LLMRouter
from db.async_client import AsyncPostgresClient

logger = logging.getLogger(__name__)

# ── Tipos de relação válidos ─────────────────────────────────────────────────
VALID_RELATION_TYPES = {"CONTEM", "PARTICIPA", "TEM_ESTADO", "TEM_RESTRICAO", "TEM_ROTINA", "POSSUI", "TEM_PROBLEMA"}

# ── Types de entidade válidos ────────────────────────────────────────────────
VALID_ENTITY_TYPES = {
    "perfil", "saude", "restricao", "rotina", "contexto",
    "refeicao", "treino", "suplemento", "objetivo", "alimento", "preferencia",
    "exercicio",
    # hardware / tech / ambiente
    "dispositivo", "hardware", "software", "problema", "configuracao", "veiculo", "imovel",
    # trabalho / estudo / finanças / social
    "trabalho", "projeto", "estudo", "financas", "pessoa", "animal",
}

# ── Prompt do extrator ───────────────────────────────────────────────────────
_WRITER_SYSTEM_PROMPT = """\
Você é um extrator de informações para um grafo de conhecimento pessoal.

Analise a conversa entre o usuário e o assistente (mensagens recentes + turno atual).
Compare com o grafo atual do usuário (fornecido abaixo).
Determine se alguma informação NOVA ou MODIFICADA deve ser salva no grafo.

## Grafo atual do usuário
{kg_context}

## O que SEMPRE deve ser salvo
Salve qualquer informação PESSOAL relevante que o usuário revele sobre si mesmo:
- **Saúde / corpo:** peso, altura, doenças, restrições, energia, sono, estresse
- **Treinos / esporte:** exercícios, planos, dias, séries, repetições, cardio
- **Alimentação:** dieta, refeições, alimentos, suplementos, restrições alimentares
- **Dispositivos / hardware:** modelos de notebook, celular, TV, carro, qualquer aparelho que o usuário POSSUI ou USA
- **Problemas técnicos:** erros, defeitos, bugs, comportamentos anômalos nos dispositivos do usuário
- **Software / ambiente:** sistema operacional, ferramentas, IDEs, apps usados no dia a dia
- **Trabalho / estudo:** cargo, empresa, projetos, cursos, área de atuação
- **Finanças / objetivos:** metas, poupança, investimentos, compras planejadas
- **Pessoas / animais:** família, amigos, pets com contexto relevante
- **Rotinas / preferências:** horários, hábitos, configurações preferidas

## Regras
- Só salve informações PESSOAIS do usuário (não fatos gerais do mundo)
- Salve quando o usuário AFIRMAR algo sobre si mesmo ("comecei X", "parei Y", "agora tenho Z", "meu X é Y", "tenho um X", "uso X")
- IMPORTANTE: Se o usuário CONFIRMAR ou APROVAR conteúdo que o assistente gerou (ex: "gostei", "salva isso",
  "perfeito", "é isso", "pode salvar", "ja salva"), trate como uma afirmação e SALVE o conteúdo relevante
  da resposta do assistente (olhe as mensagens anteriores se necessário)
- NÃO salve perguntas, dúvidas ou informações que o assistente mencionou mas o usuário NÃO confirmou
- NÃO duplique entidades que já existem no grafo (verifique pelo nome)
- Para UPDATE_ENTITY, use o `entity_name` EXATAMENTE como aparece no grafo atual
- Para CREATE_ENTITY com `attach_to`, use o contexto pai EXATAMENTE como aparece no grafo (ex: "Treino de Academia")
- Se não houver nada novo ou alterado, retorne `"operations": []`
- Máximo 8 operações por turno
- Use tipo "exercicio" para exercícios individuais de treino
- Use tipo "dispositivo" para notebooks, celulares, TVs, monitores, acessórios, etc.
- Use tipo "problema" para defeitos, bugs, erros em dispositivos ou software
- Use tipo "software" para sistemas operacionais, apps, IDEs, ferramentas

## Tipos de entidade válidos
perfil, saude, restricao, rotina, contexto, refeicao, treino, suplemento, objetivo, alimento, preferencia, exercicio,
dispositivo, hardware, software, problema, configuracao, veiculo, imovel, trabalho, projeto, estudo, financas, pessoa, animal

## Tipos de relação válidos
CONTEM, PARTICIPA, TEM_ESTADO, TEM_RESTRICAO, TEM_ROTINA, POSSUI, TEM_PROBLEMA

## Formato de saída — JSON apenas, sem texto extra
{{
  "operations": [
    {{
      "op": "CREATE_ENTITY",
      "type": "exercicio",
      "name": "Supino Reto com Barra",
      "properties": {{"series": 4, "repeticoes": "8-12", "grupo_muscular": "peito", "ordem": 1}},
      "attach_to": "Peito e Tríceps",
      "relation_type": "CONTEM"
    }},
    {{
      "op": "UPDATE_ENTITY",
      "entity_name": "Lucas Mendes",
      "properties": {{"peso_kg": 80}}
    }},
    {{
      "op": "DELETE_ENTITY",
      "entity_name": "Creatina"
    }},
    {{
      "op": "CREATE_RELATION",
      "source_name": "Whey Isolado",
      "target_name": "Pós-Treino",
      "relation_type": "CONTEM"
    }}
  ]
}}
"""


class KnowledgeGraphWriter:
    """Extrai informações novas da conversa e persiste no grafo do usuário."""

    def __init__(self, llm_router: LLMRouter, db: AsyncPostgresClient) -> None:
        self.llm = llm_router
        self.db = db

    async def extract_and_apply(
        self,
        user_id: UUID,
        user_message: str,
        assistant_response: str,
        kg_context: str,
        keywords: list[str],
        recent_history: str = "",
    ) -> list[dict[str, Any]]:
        """Analisa o turno de conversa e aplica mutações no grafo.

        Retorna lista das operações aplicadas (pode ser vazia).
        Erros são logados mas nunca propagados (fire-and-forget).
        """
        if not kg_context.strip():
            logger.debug("[KGW] Pulando: sem contexto KG disponível")
            return []

        try:
            operations = await self._extract_operations(
                user_message=user_message,
                assistant_response=assistant_response,
                kg_context=kg_context,
                recent_history=recent_history,
            )
            if not operations:
                logger.debug("[KGW] Nenhuma operação extraída para: %s", user_message[:60])
                return []

            applied: list[dict[str, Any]] = []
            async with self.db.get_session() as session:
                for op in operations:
                    try:
                        result = await self._apply_operation(session, user_id, op)
                        if result:
                            applied.append(result)
                    except Exception as e:
                        logger.warning("[KGW] Falha ao aplicar operação %s: %s", op.get("op"), e)

                await session.commit()

            logger.info("[KGW] %d operações aplicadas para user %s", len(applied), user_id)
            return applied

        except Exception as e:
            logger.warning("[KGW] extract_and_apply falhou: %s", e, exc_info=True)
            return []

    # ── Extração via LLM ─────────────────────────────────────────────────────

    async def _extract_operations(
        self,
        user_message: str,
        assistant_response: str,
        kg_context: str,
        recent_history: str = "",
    ) -> list[dict[str, Any]]:
        prompt = _WRITER_SYSTEM_PROMPT.format(kg_context=kg_context)

        history_block = ""
        if recent_history:
            history_block = f"**Mensagens recentes da conversa (contexto):**\n{recent_history}\n\n"

        user_content = (
            f"{history_block}"
            f"**Mensagem atual do usuário:**\n{user_message}\n\n"
            f"**Resposta atual do assistente:**\n{assistant_response}"
        )

        try:
            provider = self.llm.get_provider("deepseek") or self.llm.get_default_provider()
            from shared import Message, MessageRole
            response = await provider.complete(
                messages=[
                    Message(role=MessageRole.SYSTEM, content=prompt),
                    Message(role=MessageRole.USER, content=user_content),
                ],
                max_tokens=900,
                temperature=0.1,
            )
            raw = response.get_content() if hasattr(response, "get_content") else str(response)
        except Exception as e:
            logger.warning("[KGW] LLM call falhou: %s", e)
            return []

        return self._parse_operations(raw)

    def _parse_operations(self, raw: str) -> list[dict[str, Any]]:
        try:
            # Extrai JSON do raw (pode ter ```json ... ```)
            text = raw.strip()
            if "```" in text:
                start = text.find("{")
                end = text.rfind("}") + 1
                text = text[start:end]

            data = json.loads(text)
            ops = data.get("operations", [])
            if not isinstance(ops, list):
                return []

            # Valida cada operação minimamente
            valid = []
            for op in ops:
                if not isinstance(op, dict):
                    continue
                op_type = op.get("op", "")
                if op_type == "CREATE_ENTITY" and op.get("type") and op.get("name"):
                    if op["type"] not in VALID_ENTITY_TYPES:
                        logger.warning("[KGW] Tipo de entidade inválido: %s", op["type"])
                        continue
                    valid.append(op)
                elif op_type == "UPDATE_ENTITY" and op.get("entity_name") and op.get("properties"):
                    valid.append(op)
                elif op_type == "DELETE_ENTITY" and op.get("entity_name"):
                    valid.append(op)
                elif op_type == "CREATE_RELATION" and op.get("source_name") and op.get("target_name"):
                    if op.get("relation_type") not in VALID_RELATION_TYPES:
                        logger.warning("[KGW] Tipo de relação inválido: %s", op.get("relation_type"))
                        continue
                    valid.append(op)
                else:
                    logger.warning("[KGW] Operação ignorada (inválida): %s", op)

            return valid[:8]  # max 8 operações por turno

        except Exception as e:
            logger.warning("[KGW] Falha ao parsear resposta LLM: %s | raw: %s", e, raw[:200])
            return []

    # ── Executores de operação ───────────────────────────────────────────────

    async def _apply_operation(
        self, session: AsyncSession, user_id: UUID, op: dict[str, Any]
    ) -> dict[str, Any] | None:
        op_type = op.get("op")

        if op_type == "CREATE_ENTITY":
            return await self._create_entity(session, user_id, op)
        elif op_type == "UPDATE_ENTITY":
            return await self._update_entity(session, user_id, op)
        elif op_type == "DELETE_ENTITY":
            return await self._delete_entity(session, user_id, op)
        elif op_type == "CREATE_RELATION":
            return await self._create_relation(session, user_id, op)
        else:
            logger.warning("[KGW] Operação desconhecida: %s", op_type)
            return None

    async def _create_entity(
        self, session: AsyncSession, user_id: UUID, op: dict[str, Any]
    ) -> dict[str, Any] | None:
        entity_type = op["type"]
        name = op["name"]
        properties = op.get("properties") or {}
        attach_to = op.get("attach_to")
        relation_type = op.get("relation_type", "CONTEM")

        if relation_type not in VALID_RELATION_TYPES:
            relation_type = "CONTEM"

        # Verifica se já existe (evita duplicatas)
        existing = await session.execute(
            text(
                "SELECT id FROM knowledge_entity "
                "WHERE user_id = :uid AND LOWER(name) = LOWER(:name) AND type = :type"
            ),
            {"uid": str(user_id), "name": name, "type": entity_type},
        )
        if existing.fetchone():
            logger.info("[KGW] Entidade já existe, pulando CREATE: %s (%s)", name, entity_type)
            return None

        entity_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO knowledge_entity (id, user_id, type, name, properties, created_at, updated_at) "
                "VALUES (:id, :uid, :type, :name, CAST(:props AS jsonb), NOW(), NOW())"
            ),
            {
                "id": str(entity_id),
                "uid": str(user_id),
                "type": entity_type,
                "name": name,
                "props": json.dumps(properties, ensure_ascii=False),
            },
        )
        logger.info("[KGW] CREATE_ENTITY: %s (%s) props=%s", name, entity_type, properties)

        # Liga ao contexto pai se fornecido
        if attach_to:
            parent = await session.execute(
                text(
                    "SELECT id FROM knowledge_entity "
                    "WHERE user_id = :uid AND LOWER(name) = LOWER(:name)"
                ),
                {"uid": str(user_id), "name": attach_to},
            )
            parent_row = parent.fetchone()
            if parent_row:
                relation_id = uuid4()
                await session.execute(
                    text(
                        "INSERT INTO knowledge_relation (id, user_id, source_id, target_id, type, created_at) "
                        "VALUES (:id, :uid, :src, :tgt, :rtype, NOW())"
                    ),
                    {
                        "id": str(relation_id),
                        "uid": str(user_id),
                        "src": str(parent_row[0]),
                        "tgt": str(entity_id),
                        "rtype": relation_type,
                    },
                )
                logger.info("[KGW] Relação criada: %s -[%s]-> %s", attach_to, relation_type, name)
            else:
                logger.warning("[KGW] Contexto pai não encontrado: %s", attach_to)

        return {"op": "CREATE_ENTITY", "type": entity_type, "name": name, "properties": properties}

    async def _update_entity(
        self, session: AsyncSession, user_id: UUID, op: dict[str, Any]
    ) -> dict[str, Any] | None:
        entity_name = op["entity_name"]
        new_props = op["properties"]

        result = await session.execute(
            text(
                "UPDATE knowledge_entity "
                "SET properties = properties || CAST(:patch AS jsonb), updated_at = NOW() "
                "WHERE user_id = :uid AND LOWER(name) = LOWER(:name) "
                "RETURNING id, name, properties"
            ),
            {
                "uid": str(user_id),
                "name": entity_name,
                "patch": json.dumps(new_props, ensure_ascii=False),
            },
        )
        row = result.fetchone()
        if not row:
            logger.warning("[KGW] UPDATE_ENTITY: entidade não encontrada: %s", entity_name)
            return None

        logger.info("[KGW] UPDATE_ENTITY: %s patch=%s", entity_name, new_props)
        return {"op": "UPDATE_ENTITY", "entity_name": entity_name, "properties": new_props}

    async def _delete_entity(
        self, session: AsyncSession, user_id: UUID, op: dict[str, Any]
    ) -> dict[str, Any] | None:
        entity_name = op["entity_name"]

        # Busca o ID primeiro
        result = await session.execute(
            text(
                "SELECT id FROM knowledge_entity "
                "WHERE user_id = :uid AND LOWER(name) = LOWER(:name)"
            ),
            {"uid": str(user_id), "name": entity_name},
        )
        row = result.fetchone()
        if not row:
            logger.warning("[KGW] DELETE_ENTITY: entidade não encontrada: %s", entity_name)
            return None

        entity_id = str(row[0])

        # Remove relações primeiro (FK constraint)
        await session.execute(
            text(
                "DELETE FROM knowledge_relation "
                "WHERE user_id = :uid AND (source_id = :eid OR target_id = :eid)"
            ),
            {"uid": str(user_id), "eid": entity_id},
        )

        # Remove a entidade
        await session.execute(
            text(
                "DELETE FROM knowledge_entity WHERE id = :eid AND user_id = :uid"
            ),
            {"eid": entity_id, "uid": str(user_id)},
        )

        logger.info("[KGW] DELETE_ENTITY: %s", entity_name)
        return {"op": "DELETE_ENTITY", "entity_name": entity_name}

    async def _create_relation(
        self, session: AsyncSession, user_id: UUID, op: dict[str, Any]
    ) -> dict[str, Any] | None:
        source_name = op["source_name"]
        target_name = op["target_name"]
        relation_type = op["relation_type"]

        # Busca source
        src_res = await session.execute(
            text("SELECT id FROM knowledge_entity WHERE user_id = :uid AND LOWER(name) = LOWER(:name)"),
            {"uid": str(user_id), "name": source_name},
        )
        src_row = src_res.fetchone()

        # Busca target
        tgt_res = await session.execute(
            text("SELECT id FROM knowledge_entity WHERE user_id = :uid AND LOWER(name) = LOWER(:name)"),
            {"uid": str(user_id), "name": target_name},
        )
        tgt_row = tgt_res.fetchone()

        if not src_row or not tgt_row:
            logger.warning("[KGW] CREATE_RELATION: entidade não encontrada: src=%s tgt=%s", source_name, target_name)
            return None

        relation_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO knowledge_relation (id, user_id, source_id, target_id, type, created_at) "
                "VALUES (:id, :uid, :src, :tgt, :rtype, NOW()) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "id": str(relation_id),
                "uid": str(user_id),
                "src": str(src_row[0]),
                "tgt": str(tgt_row[0]),
                "rtype": relation_type,
            },
        )

        logger.info("[KGW] CREATE_RELATION: %s -[%s]-> %s", source_name, relation_type, target_name)
        return {"op": "CREATE_RELATION", "source_name": source_name, "target_name": target_name, "relation_type": relation_type}
