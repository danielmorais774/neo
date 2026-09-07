"""Core data types for the pipeline orchestrator.

These are deliberately plain dataclasses (no pydantic) — they live entirely inside
neo-core and never cross the gRPC boundary, so they carry no serialization concern.

- ``PipelineStep``  : one unit of work the orchestrator runs (an intent + its input).
- ``Handoff``       : a request, emitted at runtime by a step, to run another
                       pipeline next with a clean, self-contained input (Phase 2).
- ``StepOutcome``   : the result of running a step, including any handoffs it asked
                       for. In Phase 1 ``handoffs`` is always empty.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from shared.types import IntentType


def normalize_input(text: str) -> str:
    """Normalize an input for the anti-loop visited-set key.

    Lowercase, strip accents and collapse whitespace so that "Ligar  o AC" and
    "ligar o ac" map to the same key. Mirrors the accent handling used by the
    intent router, kept local to avoid importing the router here.
    """
    raw = (text or "").strip().lower()
    decomposed = unicodedata.normalize("NFKD", raw)
    no_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(no_accents.split())


@dataclass
class Handoff:
    """A runtime request to run another pipeline next (Phase 2).

    The ``input_text`` must be a clean, imperative, self-contained instruction —
    the target pipeline runs in isolation and never sees the original message.
    """

    target_intent: IntentType
    input_text: str
    target_skill: str | None = None
    reason: str = ""
    on_failure: str = "stop"   # "stop" | "continue"
    user_visible: bool = True
    # Self-continuation: the cognitive core handing off to ITSELF to run the next
    # concrete order in the same turn, instead of ending it with a promise
    # ("sigo tentando"). Carries a bigger budget and a work note, because unlike a
    # skill handoff the next step is a *continuation* and must know what already
    # happened — otherwise it repeats the attempt that just failed.
    is_self: bool = False


@dataclass
class PipelineStep:
    """One unit of work for the orchestrator: an intent applied to an input."""

    intent: IntentType
    input_text: str
    skill_name: str | None = None
    decision: "object | None" = None   # RoutingDecision; typed loosely to avoid import cycle
    origin: str = "classifier"          # "classifier" | "handoff" | "self" | "fallback"
    depth: int = 0
    # What the previous steps of this turn already did — injected into the
    # continuation's prompt so it never redoes finished work. Built by the step
    # runner (never by the model) from the real reply/tool trace.
    context_note: str | None = None

    def key(self) -> tuple[str, str]:
        """Stable identity for anti-loop detection (Phase 3)."""
        return (self.intent.value, normalize_input(self.input_text))

    @property
    def is_self_continuation(self) -> bool:
        """True when this step is the cognitive core continuing its own work."""
        return self.origin == "self"

    @classmethod
    def from_handoff(
        cls,
        handoff: Handoff,
        parent: "PipelineStep",
        context_note: str | None = None,
    ) -> "PipelineStep":
        """Build the next step from a handoff emitted by ``parent`` (Phase 2)."""
        return cls(
            intent=handoff.target_intent,
            input_text=handoff.input_text,
            skill_name=handoff.target_skill,
            origin="self" if handoff.is_self else "handoff",
            depth=parent.depth + 1,
            # A self-continuation inherits the accumulated note; a skill handoff
            # must stay isolated (the skill never sees this conversation).
            context_note=context_note if handoff.is_self else None,
        )


@dataclass
class StepOutcome:
    """The result of running a single step."""

    ok: bool
    final_text: str | None = None
    handoffs: list[Handoff] = field(default_factory=list)
    # One-line record of what this step actually DID (reply + tool trace), fed to
    # the next self-continuation so the cycle builds on real results instead of
    # retrying blind. Built from execution facts, never from the model's promises.
    work_note: str | None = None
