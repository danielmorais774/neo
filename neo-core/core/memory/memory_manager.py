"""
Persistent memory system using a human-readable memory.md file.

Inspired by Claude Code's memory model: the agent reads memory.md at session
start and writes important decisions / discoveries during execution.
The file is inspectable and editable by the user.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from core.memory.memory_schema import (
    ActivePlanMemory,
    Discovery,
    HistoryEntry,
    MemoryState,
)

logger = logging.getLogger(__name__)

_TEMPLATE = """\
# Neo Agent Memory

## Contexto do Projeto

## Preferências do Usuário

## Plano Ativo

## Descobertas da Sessão

## Histórico de Tarefas

## Decisões Arquiteturais
"""

# Section header regex (## Title)
_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)

# Pinned marker – lines containing this comment survive FIFO eviction
_PINNED_MARKER = "<!-- pinned -->"


def _default_memory_path() -> Path:
    """Return the default path for memory.md respecting XDG."""
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        base = Path(xdg) / "neo"
    else:
        base = Path.home() / ".neo"
    return base / "memory.md"


class MemoryManager:
    """Read/write a persistent memory.md file."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else _default_memory_path()

    # ── public API ──────────────────────────────────────────────

    def load(self) -> MemoryState:
        """Parse memory.md into a MemoryState. Creates template if missing."""
        if not self.path.exists():
            self._ensure_file()
            return MemoryState()

        try:
            raw = self.path.read_text(encoding="utf-8")
        except Exception:
            logger.warning("Failed to read %s, recreating template", self.path)
            self._ensure_file()
            return MemoryState()

        return self._parse(raw)

    def save(self, state: MemoryState) -> None:
        """Atomic write of MemoryState back to memory.md."""
        content = self._render(state)
        self._atomic_write(content)

    def append_discovery(self, fact: str) -> None:
        """Add a discovery, evicting oldest non-pinned if at capacity."""
        state = self.load()
        state.discoveries.append(
            Discovery(fact=fact, timestamp=datetime.now().isoformat(timespec="seconds"))
        )
        self._enforce_fifo(
            state.discoveries,
            state.MAX_DISCOVERIES,
            key=lambda d: _PINNED_MARKER not in d.fact,
        )
        self.save(state)

    def set_active_plan(self, goal: str, steps: list[str]) -> None:
        """Set the active plan section."""
        state = self.load()
        formatted_steps = []
        for i, s in enumerate(steps):
            prefix = "[ ]" if not s.startswith("[") else ""
            formatted_steps.append(f"{prefix} {i + 1}. {s}" if prefix else s)
        state.active_plan = ActivePlanMemory(
            goal=goal,
            steps=formatted_steps,
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
        self.save(state)

    def update_active_plan_step(self, step_index: int, status: str) -> None:
        """Mark a step as done (✓) or in-progress (→) in the active plan."""
        state = self.load()
        if not state.active_plan or step_index >= len(state.active_plan.steps):
            return
        line = state.active_plan.steps[step_index]
        # Replace status marker
        line = re.sub(r"^\[.*?\]", f"[{status}]", line)
        state.active_plan.steps[step_index] = line
        self.save(state)

    def clear_active_plan(self, final_status: str = "success") -> None:
        """Move active plan to history and clear."""
        state = self.load()
        if state.active_plan:
            self.add_history_entry(state.active_plan.goal, final_status, state=state)
            state.active_plan = None
        self.save(state)

    def add_history_entry(
        self, task: str, status: str, *, state: MemoryState | None = None
    ) -> None:
        """Prepend to history, enforcing FIFO."""
        own_state = state is None
        if own_state:
            state = self.load()
        entry = HistoryEntry(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
            task=task,
            status=status,
        )
        state.history.insert(0, entry)
        self._enforce_fifo(
            state.history,
            state.MAX_HISTORY,
            key=lambda _: True,  # no pinning for history
        )
        if own_state:
            self.save(state)

    def update_preference(self, preference: str) -> None:
        """Add a user preference line (deduplicated)."""
        state = self.load()
        if preference not in state.user_preferences:
            state.user_preferences.append(preference)
        self.save(state)

    # ── parsing ─────────────────────────────────────────────────

    def _parse(self, raw: str) -> MemoryState:
        """Parse markdown sections into MemoryState."""
        sections = self._split_sections(raw)
        state = MemoryState()

        for title, body in sections.items():
            lines = [l for l in body.strip().splitlines() if l.strip()]
            lower = title.lower().strip()

            if "contexto" in lower and "projeto" in lower:
                state.project_context = lines
            elif "preferência" in lower or "preferencia" in lower:
                state.user_preferences = lines
            elif "plano ativo" in lower:
                state.active_plan = self._parse_active_plan(lines)
            elif "descoberta" in lower:
                state.discoveries = self._parse_discoveries(lines)
            elif "histórico" in lower or "historico" in lower:
                state.history = self._parse_history(lines)
            elif "decisõ" in lower or "decisao" in lower or "decisoe" in lower:
                state.architectural_decisions = lines
            else:
                state.raw_sections[title] = body

        return state

    def _split_sections(self, raw: str) -> dict[str, str]:
        """Split markdown by ## headers, returning {title: body}."""
        sections: dict[str, str] = {}
        matches = list(_SECTION_RE.finditer(raw))
        for i, m in enumerate(matches):
            title = m.group(1)
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
            sections[title] = raw[start:end]
        return sections

    def _parse_active_plan(self, lines: list[str]) -> ActivePlanMemory | None:
        if not lines:
            return None
        goal = ""
        steps: list[str] = []
        created = ""
        for line in lines:
            if line.lower().startswith("goal:"):
                goal = line.split(":", 1)[1].strip().strip('"')
            elif line.lower().startswith("created:"):
                created = line.split(":", 1)[1].strip()
            elif line.startswith("["):
                steps.append(line)
        if not goal and not steps:
            return None
        return ActivePlanMemory(goal=goal, steps=steps, created_at=created)

    def _parse_discoveries(self, lines: list[str]) -> list[Discovery]:
        discoveries: list[Discovery] = []
        for line in lines:
            # Format: "- [timestamp] fact" or just "- fact"
            m = re.match(r"^-\s*(?:\[([^\]]+)\]\s*)?(.+)$", line)
            if m:
                ts = m.group(1) or ""
                fact = m.group(2).strip()
                discoveries.append(Discovery(fact=fact, timestamp=ts))
        return discoveries

    def _parse_history(self, lines: list[str]) -> list[HistoryEntry]:
        history: list[HistoryEntry] = []
        for line in lines:
            # Format: "- [timestamp] ✓/✗ task"
            m = re.match(r"^-\s*\[([^\]]+)\]\s*([✓✗])\s*(.+)$", line)
            if m:
                history.append(
                    HistoryEntry(
                        timestamp=m.group(1),
                        task=m.group(3).strip(),
                        status="success" if m.group(2) == "✓" else "failed",
                    )
                )
        return history

    # ── rendering ───────────────────────────────────────────────

    def _render(self, state: MemoryState) -> str:
        parts = ["# Neo Agent Memory\n"]

        parts.append("## Contexto do Projeto")
        parts.append("\n".join(state.project_context) if state.project_context else "")

        parts.append("\n## Preferências do Usuário")
        parts.append(
            "\n".join(state.user_preferences) if state.user_preferences else ""
        )

        parts.append("\n## Plano Ativo")
        if state.active_plan:
            parts.append(f'Goal: "{state.active_plan.goal}"')
            parts.append("Steps:")
            parts.append("\n".join(f"  {s}" for s in state.active_plan.steps))
            if state.active_plan.created_at:
                parts.append(f"Created: {state.active_plan.created_at}")
        
        parts.append("\n## Descobertas da Sessão")
        for d in state.discoveries:
            ts_part = f"[{d.timestamp}] " if d.timestamp else ""
            parts.append(f"- {ts_part}{d.fact}")

        parts.append("\n## Histórico de Tarefas")
        for h in state.history:
            icon = "✓" if h.status == "success" else "✗"
            parts.append(f"- [{h.timestamp}] {icon} {h.task}")

        parts.append("\n## Decisões Arquiteturais")
        parts.append(
            "\n".join(state.architectural_decisions)
            if state.architectural_decisions
            else ""
        )

        # Preserve unknown sections
        for title, body in state.raw_sections.items():
            parts.append(f"\n## {title}")
            parts.append(body.strip())

        return "\n".join(parts) + "\n"

    # ── file I/O ────────────────────────────────────────────────

    def _ensure_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(_TEMPLATE, encoding="utf-8")

    def _atomic_write(self, content: str) -> None:
        """Write via temp file + rename for atomicity, with flock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".memory_", suffix=".md.tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp, str(self.path))
        except Exception:
            # Cleanup temp on failure
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _enforce_fifo(items: list, limit: int, key=None) -> None:
        """Remove oldest non-pinned items until within limit."""
        while len(items) > limit:
            # Find first removable item (from the start = oldest)
            removed = False
            for i, item in enumerate(items):
                if key is None or key(item):
                    items.pop(i)
                    removed = True
                    break
            if not removed:
                # All pinned, force remove oldest anyway
                items.pop(0)
                break
