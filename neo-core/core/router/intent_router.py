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

ROUTER_SYSTEM_PROMPT = """\
You are an intent classifier for a desktop AI assistant called Neo.

Your job: given a user message AND the conversation history, classify which execution path to take.

## Paths

1. **DIRECT_RESPONSE** — The user is greeting, making small talk, asking general knowledge,
   requesting translations, jokes, explanations, playing games, doing quizzes,
   or anything that can be answered with text only.
   No tools or commands are needed.

2. **SERVER_TOOL** — The user needs information that requires calling an API or service
   running on the server (weather, web search, stock prices, translations via API, etc.).
   You must pick the correct tool name from the available server tools list.

3. **CLIENT_TOOL** — The user needs something executed on their LOCAL machine.
   **You have full access to the user's terminal via `run_command`.**
   Use this for ANYTHING that requires: shell commands, system info, file operations,
   running scripts, installing packages, checking processes, managing services, etc.

## Available Server Tools
{server_tools}

## Available Client Tools
{client_tools}

## CLIENT_TOOL Usage Guide

Use `run_command` (tool_name) for terminal operations. Examples:
- "quanto espaço tem no disco?" → run_command: `df -h`
- "quais processos estão rodando?" → run_command: `ps aux | head -20`
- "qual versão do python?" → run_command: `python3 --version`
- "instala o requests" → run_command: `pip install requests`
- "qual meu ip?" → run_command: `curl -s ifconfig.me`
- "mostra as variáveis de ambiente" → run_command: `env | sort`
- "cria um arquivo de teste" → run_command: `touch ~/teste.txt`
- "lista meus arquivos recentes" → run_command: `ls -lt ~ | head -20`

Use `list_directory` when the user explicitly wants to browse a folder.
Use `read_file` / `write_file` for reading or writing specific file content.
Use `get_system_info` for detailed OS/hardware information.

## CRITICAL: Path Resolution
- When the user mentions "pasta de documentos", "meus documentos", "documents" → use the Documents
  path from "Contexto do Sistema do Usuário" as `working_dir` (e.g. `/home/user/Documents`).
- When the user mentions "downloads" → use the Downloads path.
- When the user mentions "desktop", "área de trabalho" → use the Desktop path.
- **NEVER** default to the home directory when the user explicitly mentions a known folder.
- If a task requires multiple steps (e.g. "find CSV files and show their content"), combine them
  into a single shell command using `&&`, `|`, `$()`, or `xargs`.
  Example: `find ~/Documents -name '*.csv' -exec head -20 {{}} +`

## CRITICAL: Conversation Context Rules
- **ALWAYS check the conversation history** before classifying.
- If the history shows an **ongoing interactive activity** (game, quiz, roleplay, creative writing,
  storytelling, brainstorming), the user's message is almost certainly a continuation → **DIRECT_RESPONSE**.
- If the user **references something the AI previously said** ("está errado", "refaz", "tenta de novo",
  "melhora isso", "remontar"), it's about the conversation → **DIRECT_RESPONSE**.
- Words like "tabuleiro", "jogo", "placar", "turno", "rodada" in conversational context → **DIRECT_RESPONSE**.
- Only use CLIENT_TOOL when the user **explicitly** wants something done on their machine
  (run command, install, list files, check system, etc.).

## CRITICAL: Personal Knowledge Graph Rule
The system has a **Knowledge Graph** that stores the user's personal data: workout plans, exercises,
diet, supplements, routines, health info, and preferences. This data is automatically injected into
the AI's context — **no file search or tool is needed to access it**.
- When the user asks about **their own personal information** (e.g. "meu treino", "meu plano",
  "minha dieta", "meus exercícios", "minha rotina", "meus suplementos"), this is **ALWAYS DIRECT_RESPONSE**.
- **NEVER** use `search_files`, `list_directory`, or `read_file` to look for personal data —
  it lives in the Knowledge Graph, not in files.
- Only use file tools when the user **explicitly** mentions a file, path, folder, or document
  (e.g. "abre o arquivo X", "lê o documento Y", "lista a pasta Z").

## Rules
- If the message is a greeting, small talk, or general question → DIRECT_RESPONSE
- If it requires real-time data from an external API → SERVER_TOOL
- If it **explicitly** requires the user's local machine → CLIENT_TOOL
- **When in doubt and there is conversation history, prefer DIRECT_RESPONSE**
- **When in doubt and there is NO conversation history, prefer DIRECT_RESPONSE**
- Set requires_approval=true for destructive operations (delete, format, kill processes)
- Set requires_approval=false for read-only commands (ls, df, ps, cat, env, version checks)

## Memory Recovery Rule
- Set `needs_memory` to **true** when the user's message has real semantic content that
  could benefit from recalling previous conversations (questions, tasks, topics, references).
- Set `needs_memory` to **false** when the message is a simple greeting, acknowledgment,
  or phatic expression with no substantive topic (e.g. "oi", "olá", "bom dia", "ok", "tudo bem",
  "valeu", "obrigado", "hello", "hey"). In these cases, recovering old memories would only
  inject irrelevant context.

## Knowledge Keywords Rule
- Extract `knowledge_keywords`: lowercase Portuguese nouns/activities from the user message that
  could match personal context categories (food, exercise, work, health, routines, supplements,
  devices, software, problems, vehicles, finances, people, pets, etc.).
- Always use **singular** form (e.g. "treinos" → "treino", "refeições" → "refeição", "notebooks" → "notebook").
- Include hardware/device names when the user mentions something they OWN or USE
  (e.g. "notebook", "celular", "monitor", "acer predator", "iphone", "carro").
- Include problem/defect keywords when the user reports an issue with a device or software
  (e.g. "coil whine", "superaquecimento", "tela piscando", "travando").
- Maximum 6 keywords. Return empty array `[]` for greetings or messages with no personal context topic.
- These keywords are used to search the user's personal knowledge graph.

## Output Format
Respond ONLY with valid JSON (no markdown fences):
{{
  "intent": "DIRECT_RESPONSE" | "SERVER_TOOL" | "CLIENT_TOOL",
  "confidence": 0.0-1.0,
  "tool_name": "name (for SERVER_TOOL or CLIENT_TOOL registered tools)",
  "tool_arguments": {{}},
  "raw_command": "shell command (only if no registered tool fits)",
  "explanation": "brief explanation in Portuguese",
  "requires_approval": true/false,
  "needs_memory": true/false,
  "knowledge_keywords": ["keyword1", "keyword2"]
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
            provider = self.llm_router.get_provider()
            response = await provider.complete(
                messages=messages,
                tools=None,  # no function calling for classification
                temperature=0.1,
                max_tokens=300,
            )

            raw_text = response.get_content() or ""

            # Parse routing decision as before, then attach classifier metadata
            decision = self._parse_classification(raw_text)
            # Attach raw classifier response + the prompt/messages sent for later inspection
            decision.classifier_raw = raw_text
            decision.classifier_payload = {
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
