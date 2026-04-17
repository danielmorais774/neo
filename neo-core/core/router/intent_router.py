"""Intent Router — classifies user messages into execution paths.

Routes to one of:
  - DIRECT_RESPONSE: conversational / general knowledge (no tools)
  - SERVER_TOOL:     needs a tool running on the neo-core host (e.g. weather API)
  - CLIENT_TOOL:     needs a tool running on the user's desktop (shell, filesystem)
"""

import json
import logging
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

## Server Tools
{server_tools}

## Client Tools
{client_tools}

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
- `needs_plan=true` only for CLIENT_TOOL with 2+ dependent steps; always `false` for DIRECT_RESPONSE/SERVER_TOOL

## Output — valid JSON only, no markdown:
{{
  "intent": "DIRECT_RESPONSE"|"SERVER_TOOL"|"CLIENT_TOOL",
  "confidence": 0.0-1.0,
  "tool_name": "tool name or null",
  "tool_arguments": {{}},
  "raw_command": "shell cmd or null",
  "explanation": "brief in Portuguese",
  "requires_approval": false,
  "needs_memory": true,
  "knowledge_keywords": [],
  "needs_plan": false,
  "tipo_tarefa": "simples|multi_etapas|analitica|criacao"
}}
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

        base_prompt = ROUTER_SYSTEM_PROMPT.format(
            server_tools=server_tools_desc or "(none registered yet)",
            client_tools=client_tools_desc or "(none registered yet)",
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
            # Use groq llama-3.3-70b-versatile: ultra-fast Groq LPU inference
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
            decision = self._parse_classification(raw_text)
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

    def _parse_classification(self, raw_text: str) -> RoutingDecision:
        """Parse LLM JSON response into RoutingDecision."""
        text = raw_text.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse router response: {text[:200]}")
            return RoutingDecision(intent=IntentType.DIRECT_RESPONSE, confidence=0.5)

        intent_str = data.get("intent", "DIRECT_RESPONSE").upper()
        intent_map = {
            "DIRECT_RESPONSE": IntentType.DIRECT_RESPONSE,
            "SERVER_TOOL": IntentType.SERVER_TOOL,
            "CLIENT_TOOL": IntentType.CLIENT_TOOL,
        }
        intent = intent_map.get(intent_str, IntentType.DIRECT_RESPONSE)

        raw_keywords = data.get("knowledge_keywords")
        knowledge_keywords: list[str] | None = None
        if isinstance(raw_keywords, list):
            knowledge_keywords = [str(k).lower().strip() for k in raw_keywords if k] or None

        return RoutingDecision(
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
        )

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

        return RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.5,
            explanation="Fallback: resposta direta",
        )
