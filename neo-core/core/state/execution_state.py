"""
Formal execution state for plan-based task execution.

Centralizes all mutable state that was previously scattered across
ai_service.py (self._pending_traces) and plan_manager.py (self._plans).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass
class StepResult:
    """Outcome of a single plan step."""

    step_index: int
    tool_name: str
    success: bool
    output: str = ""
    error: str = ""
    llm_called: bool = False
    skipped_by_heuristic: bool = False
    latency_ms: int = 0


@dataclass
class Validation:
    """Structured validation from advance_and_refine()."""

    success: bool = True
    matches_expectation: bool = True
    discovery: str | None = None  # fact worth memorizing


@dataclass
class AdvanceResult:
    """Return type of the unified advance_and_refine() call."""

    validation: Validation
    decision: Literal["continue", "retry", "subplan", "finish"]
    reason: str = ""
    refined_step: dict | None = None  # {tool_name, tool_arguments, requires_approval, ...}
    subplan_steps: list[dict] | None = None
    retry_classification: Literal["transient", "recoverable", "fatal"] | None = None


# Retry limits per classification type
RETRY_LIMITS: dict[str, int] = {
    "transient": 3,     # network, lock, temp timeout
    "recoverable": 1,   # wrong path, permission, needs refinement
    "fatal": 0,         # invalid command, syntax error — abort
}

# Tools that only read and never modify state
READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "list_directory",
    "search_files",
    "get_system_info",
    "get_process_info",
})


@dataclass
class ExecutionState:
    """Central state for a plan-based execution session."""

    session_id: str = ""

    # The plan stack: top = active (may be sub-plan), bottom = root plan
    plan_stack: list = field(default_factory=list)  # list[ExecutionPlan]

    # Step results log
    history: list[StepResult] = field(default_factory=list)

    # Retry tracking per step index (within the active plan)
    retry_count_per_step: dict[int, int] = field(default_factory=dict)

    started_at: datetime = field(default_factory=datetime.now)

    @property
    def active_plan(self):
        """The currently executing plan (top of stack, may be a sub-plan)."""
        return self.plan_stack[-1] if self.plan_stack else None

    @property
    def root_plan(self):
        """The original top-level plan."""
        return self.plan_stack[0] if self.plan_stack else None

    def push_plan(self, plan) -> None:
        """Push a new plan (or sub-plan) onto the stack."""
        self.plan_stack.append(plan)

    def pop_plan(self):
        """Pop the top plan (completed sub-plan) and return it."""
        if self.plan_stack:
            return self.plan_stack.pop()
        return None

    def subplan_depth(self) -> int:
        """Current nesting depth (0 = root plan only)."""
        return max(0, len(self.plan_stack) - 1)

    def can_create_subplan(self) -> bool:
        """Whether another sub-plan level is allowed (max depth = 2)."""
        return self.subplan_depth() < 2

    def can_retry(self, step_idx: int, classification: str) -> bool:
        """Check if a retry is allowed given the error classification."""
        limit = RETRY_LIMITS.get(classification, 0)
        current = self.retry_count_per_step.get(step_idx, 0)
        return current < limit

    def record_retry(self, step_idx: int) -> None:
        """Increment retry counter for a step."""
        self.retry_count_per_step[step_idx] = (
            self.retry_count_per_step.get(step_idx, 0) + 1
        )

    def record_step(self, result: StepResult) -> None:
        """Log a completed step result."""
        self.history.append(result)

    def total_llm_calls(self) -> int:
        """Count how many steps actually invoked the LLM."""
        return sum(1 for r in self.history if r.llm_called)

    def total_skips(self) -> int:
        """Count how many steps were skipped by heuristic."""
        return sum(1 for r in self.history if r.skipped_by_heuristic)
