"""Intent Router — classifies user messages into execution paths.

Routes to one of:
  - DIRECT_RESPONSE: conversational / general knowledge (no tools)
  - SERVER_TOOL:     needs a tool running on the neo-core host (e.g. weather API)
  - CLIENT_TOOL:     needs a tool running on the user's desktop (shell, filesystem)
"""

import json
import logging
import re
from typing import Any

from core.llm.router import LLMRouter
from core.tools.registry import ToolRegistry
from shared import (
    IntentType,
    Message,
    MessageRole,
    RoutingDecision,
)

logger = logging.getLogger(__name__)

ROUTER_SYSTEM_PROMPT = """You are an intent classifier for Neo, a desktop AI assistant. Classify the user message into one execution path.

## Paths
- **DIRECT_RESPONSE**: conversation, general knowledge, greetings, games, translations, personal info (workout/diet/routines) — anything answerable with text only. **Default when in doubt.**
- **SERVER_TOOL**: needs real-time external data (weather, web search, stock prices). Pick tool from server list.
- **CLIENT_TOOL**: explicitly needs the user's local machine (shell, files, system info, installs). Pick tool from client list.
- **WHATSAPP_MESSAGING**: enviar QUALQUER mensagem de WhatsApp em nome do usuário. Inclui:
  - mensagens pessoais ("avisa minha mãe que vou chegar tarde", "manda um oi para Ana")
  - pedidos comerciais (pizza, delivery, farmácia, mercado, encomendas)
  - mensagens para JID/telefone direto ("manda 'oi' para 5511988217222").
  Sempre extraia em `tool_arguments` os campos `text` (texto da mensagem) e `recipient` (JID, telefone OU apelido como "minha mãe", "pai", "Ana"). NÃO interprete o apelido — passe-o cru, o pipeline resolve buscando na agenda.

## Server Tools
{server_tools}

## Client Tools
{client_tools}

## Skills
{skills}

## Tool Priority (CLIENT_TOOL)
Prefer specific tools over `run_command`:
- list files: `list_directory` | read file: `read_file` | write file: `write_file`
- find files: `search_files` | delete: `delete_path` | mkdir: `create_directory` | system info: `get_system_info`
Use `run_command` only when no specific tool fits (disk space, processes, versions, installs, network).
For multi-step tasks, use only the FIRST tool needed — follow-up steps are handled automatically.

## Key Rules
- **Conversation continuity**: if history shows an ongoing game/quiz/roleplay/creative task: DIRECT_RESPONSE
- **Back-references** ("está errado", "refaz", "melhora isso"): DIRECT_RESPONSE
- **Personal data** (treino, dieta, suplementos, rotina): DIRECT_RESPONSE (injected automatically, no file tools needed)
- **Explicit paths**: use absolute paths as-is; use folder hints from "Contexto do Sistema" (Documents, Downloads, Desktop)
- `requires_approval=true` for destructive ops (delete, kill, format); `false` for read-only
- `needs_memory=false` for greetings/acks ("oi", "ok", "valeu"); `true` for substantive topics
- `knowledge_keywords`: up to 6 singular PT nouns from user message (devices, health, food, finance, etc.); `[]` for greetings
- `needs_plan=true` only for CLIENT_TOOL with 2+ dependent steps; always `false` for DIRECT_RESPONSE/SERVER_TOOL/WHATSAPP_MESSAGING

## Output — valid JSON only, no markdown:
{{
  "intent": "DIRECT_RESPONSE"|"SERVER_TOOL"|"CLIENT_TOOL"|"WHATSAPP_MESSAGING",
  "confidence": 0.0-1.0,
  "tool_name": "tool name or null",
  "tool_arguments": {{}},
  "raw_command": "shell cmd or null",
  "explanation": "brief in Portuguese",
  "requires_approval": false,
  "needs_memory": true,
  "knowledge_keywords": [],
  "needs_plan": false,
  "tipo_tarefa": "simples|multi_etapas|analitica|criacao",
  "skill_name": "whatsapp_messaging or null"
}}

## WHATSAPP_MESSAGING — exemplos de extração

User: "manda uma mensagem para minha mãe que vou chegar mais tarde"
→ tool_arguments: {{"text": "vou chegar mais tarde", "recipient": "minha mãe"}}

User: "avisa o pai que cheguei"
→ tool_arguments: {{"text": "cheguei", "recipient": "pai"}}

User: "manda whatsapp pro 5511988217222: oi tudo bem?"
→ tool_arguments: {{"text": "oi tudo bem?", "recipient": "5511988217222"}}

User: "pede uma pizza no Domino's"
→ tool_arguments: {{"text": "Olá, gostaria de fazer um pedido", "recipient": "Domino's"}}
"""


class IntentRouter:
    """Classifies user messages and routes to the correct execution path."""

    def __init__(
        self,
        llm_router: LLMRouter,
        tool_registry: ToolRegistry,
    ) -> None:
        self.llm_router = llm_router
        self.tool_registry = tool_registry

    async def classify(
        self,
        user_message: str,
        conversation_history: list[dict[str, str]] | None = None,
        system_context: str | None = None,
    ) -> RoutingDecision:
        """Classify user intent and return a routing decision.

        Args:
            user_message: The user's message text.
            conversation_history: Recent messages as [{role, content}] for context.
            system_context: Optional OS/environment context string to append to the prompt.

        Returns:
            RoutingDecision with intent, tool info, etc.
        """
        # Build tool descriptions for the prompt
        server_tools_desc = self._format_tools("server-side")
        client_tools_desc = self._format_tools("client-side")
        skills_desc = self._format_skills()

        base_prompt = ROUTER_SYSTEM_PROMPT.format(
            server_tools=server_tools_desc or "(none registered yet)",
            client_tools=client_tools_desc or "(none registered yet)",
            skills=skills_desc or "(none registered yet)",
        )

        # Inject system context (OS, paths) when provided by the client
        if system_context:
            base_prompt += f"\n\n## Contexto do Sistema do Usuário\n{system_context}"

        system_prompt = base_prompt

        # Build messages
        messages: list[Message] = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
        ]

        # Add last few messages of history for context
        if conversation_history:
            for msg in conversation_history[-6:]:
                role = MessageRole(msg["role"]) if msg["role"] in ("user", "assistant") else MessageRole.USER
                messages.append(Message(role=role, content=msg["content"]))

        # Only append current message if not already the last in history
        last_hist = conversation_history[-1] if conversation_history else None
        if not last_hist or last_hist.get("content") != user_message or last_hist.get("role") != "user":
            messages.append(Message(role=MessageRole.USER, content=user_message))

        try:
            # Use groq for intent classification (fast inference)
            provider_name = "groq"
            model_name = "llama-3.3-70b-versatile"
            provider = self.llm_router.get_provider(provider_name)
            response = await provider.complete(
                messages=messages,
                tools=None,  # no function calling for classification
                model=model_name,
                temperature=0.1,
                max_tokens=300,
            )

            raw_text = response.get_content() or ""

            # Parse routing decision as before, then attach classifier metadata
            decision = self._parse_classification(raw_text, user_message=user_message)
            self._apply_memory_heuristics(decision, user_message)
            # Attach raw classifier response + the prompt/messages sent for later inspection
            decision.classifier_raw = raw_text
            decision.classifier_payload = {
                "provider": provider_name,
                "model": model_name,
                "messages": [
                    {"role": m.role.value if hasattr(m.role, 'value') else str(m.role), "content": m.content[:4000]}
                    for m in messages
                ],
                "raw_response": raw_text,
            }
            return decision

        except Exception as e:
            logger.exception(f"Intent classification failed: {e}")
            # Return a fallback decision but include the prompt we tried to send (if any)
            fallback = self._fallback_classify(user_message)
            self._apply_memory_heuristics(fallback, user_message)
            fallback.classifier_payload = {"messages": [
                {"role": m.role.value if hasattr(m.role, 'value') else str(m.role), "content": m.content[:4000]}
                for m in messages
            ], "error": str(e)}
            return fallback

    def _format_tools(self, execution_type: str) -> str:
        """Format tool list for the system prompt."""
        tools = [t for t in self.tool_registry.get_all() if t.execution == execution_type]
        if not tools:
            return ""
        lines = []
        for t in tools:
            params_desc = ", ".join(
                f"{name}: {p.type}" for name, p in t.parameters.items()
            )
            lines.append(f"- {t.name}: {t.description} ({params_desc})")
        return "\n".join(lines)

    def _format_skills(self) -> str:
        """Format registered skills for the system prompt."""
        from core.skills.manifest import SkillManifest
        skills = SkillManifest.all()
        if not skills:
            return ""
        lines = []
        for name, manifest in skills.items():
            lines.append(f"- {name}: {manifest.description} (keywords: {', '.join(manifest.keywords)})")
        return "\n".join(lines)

    def _parse_classification(self, raw_text: str, user_message: str = "") -> RoutingDecision:
        """Parse LLM JSON response into RoutingDecision."""
        text = raw_text.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # LLM returned prose instead of JSON — try to extract an embedded JSON object.
            json_text = self._extract_embedded_json(text)
            if json_text:
                try:
                    data = json.loads(json_text)
                    logger.warning("Router: extracted JSON from prose response")
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse router response (bad embedded JSON): {text[:200]}")
                    fallback = self._fallback_classify(user_message or raw_text)
                    self._apply_memory_heuristics(fallback, user_message or raw_text)
                    return fallback
            else:
                logger.warning(f"Failed to parse router response (no JSON found): {text[:200]}")
                fallback = self._fallback_classify(user_message or raw_text)
                self._apply_memory_heuristics(fallback, user_message or raw_text)
                return fallback

        intent_str = data.get("intent", "DIRECT_RESPONSE").upper()
        intent_map = {
            "DIRECT_RESPONSE": IntentType.DIRECT_RESPONSE,
            "SERVER_TOOL": IntentType.SERVER_TOOL,
            "CLIENT_TOOL": IntentType.CLIENT_TOOL,
            "WHATSAPP_MESSAGING": IntentType.WHATSAPP_MESSAGING,
            "WHATSAPP_ORDER": IntentType.WHATSAPP_MESSAGING,
        }
        intent = intent_map.get(intent_str, IntentType.DIRECT_RESPONSE)

        raw_keywords = data.get("knowledge_keywords")
        knowledge_keywords: list[str] | None = None
        if isinstance(raw_keywords, list):
            knowledge_keywords = [str(k).lower().strip() for k in raw_keywords if k] or None

        decision = RoutingDecision(
            intent=intent,
            confidence=float(data.get("confidence", 0.8)),
            tool_name=data.get("tool_name"),
            tool_arguments=data.get("tool_arguments"),
            explanation=data.get("explanation"),
            requires_approval=data.get("requires_approval", intent == IntentType.CLIENT_TOOL),
            raw_command=data.get("raw_command"),
            needs_memory=data.get("needs_memory", True),
            knowledge_keywords=knowledge_keywords,
            needs_plan=data.get("needs_plan", False),
            tipo_tarefa=data.get("tipo_tarefa", "simples"),
            skill_name=data.get("skill_name"),
        )
        return decision

    @staticmethod
    def _extract_embedded_json(text: str) -> str | None:
        start = text.find("{")
        while start >= 0:
            depth = 0
            in_string = False
            escape = False
            for index in range(start, len(text)):
                char = text[index]
                if escape:
                    escape = False
                    continue
                if char == "\\":
                    escape = True
                    continue
                if char == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:index + 1]
                        if '"intent"' in candidate:
                            return candidate
                        break
            start = text.find("{", start + 1)
        return None

    def _fallback_classify(self, message: str) -> RoutingDecision:
        """Keyword-based fallback when LLM is unavailable."""
        text = (message or "").lower()
        # Normalize accents
        for src, dst in {
            "á": "a", "à": "a", "â": "a", "ã": "a",
            "é": "e", "ê": "e", "í": "i",
            "ó": "o", "ô": "o", "õ": "o",
            "ú": "u", "ç": "c",
        }.items():
            text = text.replace(src, dst)

        # Client-side indicators
        client_tokens = [
            "arquivos", "files", "pastas", "folders", "diretorio", "directory",
            "downloads", "desktop", "home", "disco", "disk", "espaco", "space",
            "tamanho", "size", "processos", "processes", "versao", "version",
            "conteudo", "listar", "lista", "mostra", "terminal", "comando",
        ]
        # Multi-step file task: "leia ... e crie" or "ler ... escrever"
        file_read_write = (
            ("leia" in text or "ler" in text or "leia" in text or "read" in text)
            and ("crie" in text or "escreva" in text or "criar" in text or "write" in text or "salve" in text)
        )
        if file_read_write:
            return RoutingDecision(
                intent=IntentType.CLIENT_TOOL,
                confidence=0.7,
                explanation="Fallback: leitura + escrita de arquivos detectada",
                requires_approval=False,
                needs_plan=True,
            )
        if sum(1 for t in client_tokens if t in text) >= 2:
            return RoutingDecision(
                intent=IntentType.CLIENT_TOOL,
                confidence=0.6,
                raw_command='ls -la "$HOME"',
                explanation="Fallback: detectado pedido de operação local",
                requires_approval=True,
            )

        # Server-side indicators
        server_tokens = [
            "clima", "tempo", "weather", "temperatura", "previsao",
            "cotacao", "dolar", "bitcoin", "busca", "search", "pesquisa",
        ]
        if sum(1 for t in server_tokens if t in text) >= 1:
            return RoutingDecision(
                intent=IntentType.SERVER_TOOL,
                confidence=0.5,
                explanation="Fallback: detectado pedido de dados externos",
            )

        # WhatsApp indicators
        whatsapp_tokens = [
            "whatsapp", "wpp", "zap", "mensagem", "mensagens",
            "manda", "mandar", "envia", "enviar", "texto",
            "contato", "telefone", "celular",
        ]
        if sum(1 for t in whatsapp_tokens if t in text) >= 1:
            return RoutingDecision(
                intent=IntentType.WHATSAPP_MESSAGING,
                confidence=0.7,
                skill_name="whatsapp_messaging",
                explanation="Fallback: detectado pedido via WhatsApp",
            )

        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.5,
            explanation="Fallback: resposta direta",
        )
        self._apply_memory_heuristics(decision, message)
        return decision

    def _apply_memory_heuristics(self, decision: RoutingDecision, message: str) -> None:
        """Backend guardrail: factual personal messages must not lose memory flags."""
        text = (message or "").lower()
        normalized = text
        for src, dst in {
            "á": "a", "à": "a", "â": "a", "ã": "a",
            "é": "e", "ê": "e", "í": "i",
            "ó": "o", "ô": "o", "õ": "o",
            "ú": "u", "ç": "c",
        }.items():
            normalized = normalized.replace(src, dst)

        memory_terms = {
            "academia": "academia",
            "treino": "treino",
            "exercicio": "exercicio",
            "exercicios": "exercicio",
            "supino": "supino",
            "crucifixo": "crucifixo",
            "crusifixo": "crucifixo",
            "triceps": "triceps",
            "biceps": "biceps",
            "peito": "peito",
            "costa": "costas",
            "costas": "costas",
            "perna": "perna",
            "ombro": "ombro",
            "reps": "reps",
            "repeticoes": "reps",
            "sessoes": "series",
            "series": "series",
            "quilos": "peso",
            "kg": "peso",
            "meu nome": "nome",
            "menu nome": "nome",
            "eu me chamo": "nome",
            "meu pc": "pc",
            "minha irma": "familia",
        }
        weekdays = ("segunda", "terca", "terça", "quarta", "quinta", "sexta", "sabado", "sábado", "domingo")
        has_numeric_training = bool(re.search(r"\b\d+\s*(kg|quilo|quilos|reps?|repeticoes|repetições|sessoes|sessões|series|séries)\b", normalized))
        detected = [kw for token, kw in memory_terms.items() if token in normalized]
        if any(day in normalized for day in weekdays):
            detected.extend(["rotina", "treino"])
        if has_numeric_training:
            detected.extend(["exercicio", "peso", "series", "reps"])

        if not detected:
            return

        decision.needs_memory = True
        existing = list(decision.knowledge_keywords or [])
        for kw in detected:
            if kw not in existing:
                existing.append(kw)
        decision.knowledge_keywords = existing[:6] or None
        if decision.intent == IntentType.DIRECT_RESPONSE and decision.confidence < 0.65:
            decision.confidence = 0.65
