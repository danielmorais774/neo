"""Execution policy for pipelines reached via a runtime handoff (Phase 6).

A handoff lets the cognitive core trigger another pipeline. For local/reversible
actions (lights, climate) that may run immediately. For outward/irreversible
actions (sending a WhatsApp, unlocking a door) it must go through the skill's own
human-confirmation flow — a handoff must never silently send something on the
user's behalf. WhatsApp messaging already gates sends behind a mission/confirmation
step; this policy makes the intent explicit and auditable.
"""

from __future__ import annotations

from shared.types import IntentType

# "auto"    → local/reversible; may execute immediately.
# "confirm" → outward/irreversible; must pass through human confirmation.
HANDOFF_EXECUTION_POLICY: dict[IntentType, str] = {
    IntentType.HOME_ASSISTANT: "auto",
    IntentType.WHATSAPP_MESSAGING: "confirm",
}


def handoff_requires_confirmation(intent: IntentType) -> bool:
    """True when a handoff to ``intent`` must not auto-execute.

    Unknown targets default to "confirm" (fail safe).
    """
    return HANDOFF_EXECUTION_POLICY.get(intent, "confirm") == "confirm"
