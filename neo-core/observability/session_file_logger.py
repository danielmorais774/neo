"""Session File Logger — writes a detailed Markdown log per session.

Each session gets its own stable .md file at /app/logs/session-<id>.md (inside
Docker) or neo-core/logs/session-<id>.md (local dev), so all interactions for
the same session append to a single file.

The log captures every step of the pipeline:
  - Input received
  - Session resolution (new vs existing, topic rotation)
  - History retrieval
  - Intent classification (full payload)
  - Context recovery (hybrid search results)
  - LLM prompt (full messages array)
  - LLM response (full text)
  - Tool requests and results
  - Error recovery steps
  - Final response
"""

from __future__ import annotations

import json
import os
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from observability import session_log_payload as fmt

logger = logging.getLogger(__name__)

# Resolve logs directory: /app/logs inside Docker, fallback to neo-core/logs locally
_LOGS_DIR = Path(os.environ.get("SESSION_LOGS_DIR", "/app/logs"))
if not _LOGS_DIR.exists():
    _LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _safe_json(obj: Any, indent: int = 2) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=indent, default=str)
    except Exception:
        return str(obj)


class SessionFileLogger:
    """Append-only Markdown logger for a single session."""

    def __init__(
        self,
        session_id: UUID | str,
        *,
        file_prefix: str = "session",
        include_timestamp: bool = False,
        title: str = "Session Log",
    ) -> None:
        self.session_id = str(session_id)
        self.title = title
        if include_timestamp:
            # Include a timestamp in the filename so files sort naturally newest-first
            # and each process run creates a distinct file.
            _now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
            filename = f"{file_prefix}-{_now}-{self.session_id}.md"
        else:
            filename = f"{file_prefix}-{self.session_id}.md"
        self._path = _LOGS_DIR / filename
        self._ensure_header()
        self._interaction = self._load_existing_interaction_count()

    def _ensure_header(self) -> None:
        if not self._path.exists():
            self._write(
                f"# {self.title}: `{self.session_id}`\n\n"
                f"**Criado em:** {_ts()}\n\n"
                f"---\n\n"
            )

    def _write(self, text: str) -> None:
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            logger.warning(f"[SessionFileLogger] Failed to write log: {e}")

    def _load_existing_interaction_count(self) -> int:
        try:
            if not self._path.exists():
                return 0
            content = self._path.read_text(encoding="utf-8")
            matches = re.findall(r"^## Interação (\d+)$", content, flags=re.MULTILINE)
            if not matches:
                return 0
            return max(int(value) for value in matches)
        except Exception as e:
            logger.warning(f"[SessionFileLogger] Failed to load interaction count: {e}")
            return 0

    # ── Public API ──────────────────────────────────────────────

    def new_interaction(self, user_message: str, desktop_session_id: str | None = None, metadata: dict | None = None) -> None:
        self._interaction += 1
        self._interaction_start = datetime.now(timezone.utc)
        self._step_timings: list[tuple[str, float]] = []
        self._rodada = 0
        self._write(
            f"## Interação {self._interaction}\n\n"
            f"**⏱ Request recebida:** `{self._interaction_start.strftime('%H:%M:%S.%f')[:-3]} UTC`  "
            f"({self._interaction_start.strftime('%Y-%m-%d')})\n\n"
        )
        if desktop_session_id:
            self._write(f"**Desktop session_id:** `{desktop_session_id}`\n\n")
        self._write(
            f"### 1. Input Recebido\n\n"
            f"```\n{user_message}\n```\n\n"
        )
        if metadata:
            self._write(
                f"#### Metadata do Cliente\n\n"
                f"```json\n{_safe_json(metadata)}\n```\n\n"
            )

    def log_session_resolution(
        self,
        db_session_id: str,
        message_count: int,
        is_new: bool = False,
        rotated_from: str | None = None,
    ) -> None:
        status = "**Nova sessão criada**" if is_new else f"Sessão existente (mensagens: {message_count})"
        self._write(f"### 2. Resolução de Sessão\n\n")
        self._write(f"- **DB session_id:** `{db_session_id}`\n")
        self._write(f"- **Status:** {status}\n")
        if rotated_from:
            self._write(f"- **Rotacionada de:** `{rotated_from}` (mudança de tópico detectada)\n")
        self._write("\n")

    def log_topic_check(self, similarity: float | None, threshold: float, rotated: bool, reason: str = "",
                        message_count: int = 0, min_required: int = 0, cooldown_minutes: int = 0,
                        elapsed_minutes: float | None = None, new_message: str = "",
                        context_text: str = "", duration_ms: float = 0) -> None:
        self._write(f"### 2.1 Detecção de Tópico ({duration_ms:.0f}ms)\n\n")
        self._write(f"- **Mensagens na sessão:** {message_count} (mínimo necessário: {min_required})\n")
        if cooldown_minutes > 0:
            elapsed_str = f"{elapsed_minutes:.1f}" if elapsed_minutes is not None else "?"
            self._write(f"- **Cooldown:** {cooldown_minutes}min (tempo desde última atividade: {elapsed_str}min)\n")
        if new_message:
            self._write(f"- **Tamanho da mensagem:** {len(new_message.strip())} chars (mínimo: 30)\n")
        if similarity is not None:
            self._write(f"- **Similaridade coseno:** `{similarity:.4f}`\n")
        self._write(f"- **Threshold:** `{threshold}`\n")
        self._write(f"- **Rotacionou:** {'✅ Sim' if rotated else '❌ Não'}\n")
        if reason:
            self._write(f"- **Razão:** {reason}\n")
        if context_text:
            self._write(f"\n<details>\n<summary>Contexto usado para embedding (últimas 5 msgs)</summary>\n\n```\n{context_text}\n```\n\n</details>\n\n")
        self._write("\n")

    def log_history(self, history: list[dict[str, str]]) -> None:
        self._write(f"### 3. Histórico Recuperado ({len(history)} mensagens)\n\n")
        if not history:
            self._write("_Nenhuma mensagem no histórico._\n\n")
            return
        for i, msg in enumerate(history):
            role = msg.get("role", "?")
            content = msg.get("content", "")
            self._write(f"**[{i}] {role}:**\n```\n{content}\n```\n\n")

    def log_intent_classification(
        self,
        decision_data: dict,
        duration_ms: float = 0,
        embed_ms: float = 0,
        hist_ms: float = 0,
    ) -> None:
        """Dump the full RoutingDecision as JSON — no cherry-picking, new fields appear automatically."""
        self._write(f"### 4. Classificação de Intent\n\n")
        self._write(f"- **Duração classify:** {duration_ms:.1f}ms\n")
        if embed_ms:
            self._write(f"- **Duração embedding:** {embed_ms:.1f}ms\n")
        if hist_ms:
            self._write(f"- **Duração histórico:** {hist_ms:.1f}ms\n")
        # Separate large blobs so the main block stays readable
        raw_response = decision_data.pop("classifier_raw", None)
        classifier_payload = decision_data.pop("classifier_payload", None)
        # Show LLM provider/model prominently before the JSON block
        if classifier_payload:
            llm_provider = classifier_payload.get("provider", "?")
            llm_model = classifier_payload.get("model", "?")
            self._write(f"- **LLM:** `{llm_provider}` / `{llm_model}`\n")
        self._write("\n")
        self._write(f"```json\n{_safe_json(decision_data)}\n```\n\n")
        if classifier_payload:
            self._write(
                f"<details>\n<summary>Payload do classificador (prompt)</summary>\n\n"
                f"```json\n{_safe_json(classifier_payload)}\n```\n\n</details>\n\n"
            )
        if raw_response:
            self._write(
                f"<details>\n<summary>Resposta raw do classificador</summary>\n\n"
                f"```\n{raw_response}\n```\n\n</details>\n\n"
            )

    def log_context_recovery(self, results: list[Any]) -> None:
        self._write(f"### 5. Recuperação de Contexto ({len(results) if results else 0} resultados)\n\n")
        if not results:
            self._write("_Nenhum contexto recuperado._\n\n")
            return
        for i, ctx in enumerate(results):
            content = getattr(ctx, "content", str(ctx))
            score = getattr(ctx, "final_score", 0)
            source = getattr(ctx, "source", "?")
            session_id = getattr(ctx, "session_id", None)
            created_at = getattr(ctx, "created_at", None)
            meta_parts = [f"source=`{source}`", f"score={score:.3f}"]
            if session_id:
                meta_parts.append(f"session=`{str(session_id)[:8]}…`")
            if created_at:
                meta_parts.append(f"criado={created_at.strftime('%Y-%m-%d %H:%M') if hasattr(created_at, 'strftime') else str(created_at)}")
            meta = ", ".join(meta_parts)
            self._write(f"**[{i}]** ({meta})\n```\n{content}\n```\n\n")

    def log_llm_request(
        self,
        stage: str,
        messages: list[Any],
        model: str = "",
        provider: str = "",
        temperature: float | None = None,
        tools: list[Any] | None = None,
        tool_choice: Any = None,
    ) -> None:
        """O que foi enviado ao modelo — INCLUSIVE a mesa de tools.

        O antigo bloco "RAW payload" se anunciava como "completo" e durante
        muito tempo não era: `tools` e `tool_choice` ficavam de fora. A pergunta que o diário
        então não respondia é a primeira de qualquer investigação de "por que
        ele não chamou X?" — se X estava na mesa. Nas sessões 0cd866a1 e
        e0905aae só deu para afirmar que `browser_local_task` estava disponível
        porque um turno POSTERIOR a chamou; o log, sozinho, não dizia.

        `tool_choice` entra pelo mesmo motivo, do outro lado: um round com
        `required` explica uma chamada de tool que de outro jeito pareceria
        iniciativa do modelo.

        Cada mensagem vai inteira só na 1ª vez que aparece no arquivo; depois
        vira uma linha ``= #sha`` (formato e medição em
        ``observability/session_log_payload.py``). O antigo bloco "⚡ RAW
        payload" saiu: repetia as mensagens logo abaixo dele, e
        ``scripts/payload_do_log.py`` remonta o mesmo JSON a partir delas.
        """
        self._esquecer_se_o_arquivo_mudou()
        self._rodada = getattr(self, "_rodada", 0) + 1
        onde = f"Interação {self._interaction}, rodada {self._rodada}"
        partes: list[str] = [f"### 6. LLM Request — {stage} (rodada {self._rodada})\n\n"]
        partes.append(f"- **Provider:** {provider}\n")
        partes.append(f"- **Model:** {model}\n")
        if temperature is not None:
            partes.append(f"- **Temperature:** {temperature}\n")
        partes.append(f"- **Total mensagens:** {len(messages)}\n")
        nomes = [
            str(getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else t))
            for t in (tools or [])
        ]
        if not nomes:
            partes.append("- **Tools na mesa (0):** _nenhuma_\n")
        else:
            marca = fmt.sha("\n".join(nomes))
            if marca in self._tools_vistas:
                partes.append(
                    f"- **Tools na mesa ({len(nomes)}):** = #{marca} "
                    f"(iguais às da {self._tools_vistas[marca]})\n"
                )
            else:
                self._tools_vistas[marca] = onde
                partes.append(
                    f"- **Tools na mesa ({len(nomes)}):** "
                    + ", ".join(f"`{n}`" for n in nomes)
                    + f" · #{marca}\n"
                )
        if tool_choice:
            partes.append(f"- **tool_choice:** `{fmt.json_compacto(tool_choice)}`\n")
        partes.append("\n")

        for i, msg in enumerate(messages):
            if hasattr(msg, "role"):
                role = msg.role.value if hasattr(msg.role, "value") else str(msg.role)
                content = msg.content or ""
            elif isinstance(msg, dict):
                role = msg.get("role", "?")
                content = msg.get("content", "")
            else:
                role = "?"
                content = str(msg)
            partes.append(self._bloco_da_mensagem(i, str(role), str(content), f"{onde}, [{i}]"))

        self._write("".join(partes))
        self._marcar_tamanho()

    # ── dedup das mensagens (ver observability/session_log_payload.py) ──

    def _bloco_da_mensagem(self, i: int, role: str, content: str, onde: str) -> str:
        if len(content) < fmt.MIN_DEDUP:
            return fmt.bloco_inteiro(i, role, content)
        marca = fmt.sha(content)
        if marca in self._msgs_vistas:
            return fmt.linha_referencia(i, role, content, marca, self._msgs_vistas[marca])
        self._msgs_vistas[marca] = onde
        if role != "system":
            return fmt.bloco_inteiro(i, role, content, marca)
        # System muda aos pedaços entre rodadas (caderno, PKS, hora): a versão
        # anterior é a do mesmo bloco, reconhecido pela 1ª linha.
        chave = content.split("\n", 1)[0][:80]
        anterior = self._ultimo_system.get(chave)
        self._ultimo_system[chave] = (marca, content)
        if anterior is not None:
            diff = fmt.fazer_diff(anterior[1], content)
            if diff is not None:
                return fmt.bloco_diff(
                    i, role, content, marca, anterior[0], diff, self._msgs_vistas[anterior[0]]
                )
        return fmt.bloco_inteiro(i, role, content, marca)

    def _esquecer_se_o_arquivo_mudou(self) -> None:
        """Arquivo apagado ou encolhido: as marcas antigas não estão mais nele.

        Uma referência só vale se o original estiver no MESMO arquivo. Se alguém
        limpou o diário com o processo de pé, a próxima rodada volta a gravar
        tudo inteiro em vez de apontar para o vazio.
        """
        if not hasattr(self, "_msgs_vistas"):
            self._msgs_vistas: dict[str, str] = {}
            self._tools_vistas: dict[str, str] = {}
            self._ultimo_system: dict[str, tuple[str, str]] = {}
            self._tamanho_visto = 0
        try:
            tamanho = self._path.stat().st_size
        except OSError:
            tamanho = -1
        if tamanho < self._tamanho_visto:
            self._msgs_vistas.clear()
            self._tools_vistas.clear()
            self._ultimo_system.clear()
            self._tamanho_visto = 0

    def _marcar_tamanho(self) -> None:
        try:
            self._tamanho_visto = self._path.stat().st_size
        except OSError:
            self._tamanho_visto = 0

    def log_llm_response(
        self,
        stage: str,
        response: str,
        duration_ms: float = 0,
        model: str = "",
    ) -> None:
        self._write(f"### 7. LLM Response — {stage}\n\n")
        self._write(f"- **Duração:** {duration_ms:.1f}ms\n")
        if model:
            self._write(f"- **Model:** {model}\n")
        self._write(f"- **Tamanho:** {len(response)} chars\n\n")
        self._write(f"```\n{response}\n```\n\n")

    def log_tool_request(
        self,
        tool_name: str,
        arguments: dict | None = None,
        requires_approval: bool = False,
        command: str | None = None,
        execution: str | None = None,
    ) -> None:
        self._write(f"### Tool Request\n\n")
        self._write(f"- **Tool:** `{tool_name}`\n")
        if execution:
            label = {"client-side": "🖥 neo-desktop (client)", "server-side": "☁️ neo-core (server)"}.get(execution, execution)
            self._write(f"- **Executado em:** {label}\n")
        if command:
            self._write(f"- **Comando:** `{command}`\n")
        self._write(f"- **Requer aprovação:** {'Sim' if requires_approval else 'Não'}\n")
        if arguments:
            self._write(f"- **Argumentos:**\n```json\n{_safe_json(arguments)}\n```\n\n")
        self._write("\n")

    def log_tool_result(
        self,
        tool_name: str,
        success: bool,
        result: str | None = None,
        error: str | None = None,
        duration_ms: float = 0,
        execution: str | None = None,
    ) -> None:
        status = "Sucesso" if success else "Falha"
        self._write(f"### Tool Result\n\n")
        self._write(f"- **Tool:** `{tool_name}`\n")
        if execution:
            label = {"client-side": "🖥 neo-desktop (client)", "server-side": "☁️ neo-core (server)"}.get(execution, execution)
            self._write(f"- **Executado em:** {label}\n")
        self._write(f"- **Status:** {status}\n")
        self._write(f"- **Duração:** {duration_ms:.1f}ms\n")
        if error:
            self._write(f"- **Erro:** {error}\n")
        if result:
            if isinstance(result, (dict, list, tuple)):
                self._write(f"\n```json\n{_safe_json(result)}\n```\n\n")
            else:
                self._write(f"\n```\n{result}\n```\n\n")
        self._write("\n")

    def log_error_recovery(
        self,
        attempt: int,
        error_type: str,
        action: str,
        details: dict | None = None,
    ) -> None:
        self._write(f"### Recovery (tentativa {attempt})\n\n")
        self._write(f"- **Tipo de erro:** `{error_type}`\n")
        self._write(f"- **Ação decidida:** `{action}`\n")
        if details:
            self._write(f"- **Detalhes:**\n```json\n{_safe_json(details)}\n```\n\n")
        self._write("\n")

    def log_final_response(self, response: str, intent: str, iterations: int = 1) -> None:
        self._write(f"### 8. Resposta Final\n\n")
        self._write(f"- **Intent:** `{intent}`\n")
        self._write(f"- **Iterações:** {iterations}\n")
        self._write(f"- **Tamanho:** {len(response)} chars\n\n")
        self._write(f"```\n{response}\n```\n\n")

    def record_step_timing(self, step: str, ms: float) -> None:
        """Accumulate a timing entry for the end-of-interaction summary table."""
        if not hasattr(self, "_step_timings"):
            self._step_timings = []
        self._step_timings.append((step, ms))

    def log_timing_summary(self, total_ms: float) -> None:
        """Write response timestamp and a markdown timing table at the end of the interaction."""
        now = datetime.now(timezone.utc)
        self._write(f"### ⏱ Resumo de Tempo\n\n")
        self._write(f"**Resposta enviada:** `{now.strftime('%H:%M:%S.%f')[:-3]} UTC`\n\n")
        timings = getattr(self, "_step_timings", [])
        if timings:
            self._write("| Etapa | Tempo |\n")
            self._write("|-------|------:|\n")
            for step, ms in timings:
                self._write(f"| {step} | `{ms:.0f}ms` |\n")
            self._write(f"| **Total (neo-core)** | **`{total_ms:.0f}ms`** |\n")
            self._write("\n")
        self._write("---\n\n")

    def log_message_saved(self, role: str, content_preview: str, session_id: str) -> None:
        self._write(f"### Mensagem salva no DB\n\n")
        self._write(f"- **Role:** `{role}`\n")
        self._write(f"- **Sessão:** `{session_id}`\n")
        self._write(f"- **Conteúdo:** {content_preview}\n\n")

    def log_pipeline_step(
        self,
        index: int,
        intent: str,
        origin: str,
        input_text: str,
    ) -> None:
        """Header marking the start of one orchestrator step (chain element)."""
        self._write(f"### 🔗 Pipeline — Step {index} (`{intent}`, origem `{origin}`)\n\n")
        self._write(f"- **Input do step:** {input_text}\n\n")

    def log_event(self, title: str, details: Any = "") -> None:
        """Log a generic event."""
        self._write(f"### {title}\n\n")
        if details:
            if isinstance(details, (dict, list, tuple)):
                self._write(f"```json\n{_safe_json(details)}\n```\n\n")
            else:
                self._write(f"{details}\n\n")


# ── Cache of active loggers (one per session) ──────────────────

_active_loggers: dict[str, SessionFileLogger] = {}


def get_session_logger(session_id: UUID | str) -> SessionFileLogger:
    """Get or create a SessionFileLogger for the given session."""
    key = str(session_id)
    if key not in _active_loggers:
        _active_loggers[key] = SessionFileLogger(key)
    return _active_loggers[key]
