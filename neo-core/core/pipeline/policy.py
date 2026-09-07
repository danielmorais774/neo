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
    # Playing music is reversible/low-risk → no confirmation gate.
    IntentType.MUSIC: "auto",
    # Self-continuation: the cognitive core running itself again inside its own
    # sandbox. Nothing leaves the VPS and nothing touches the user's machine —
    # every outward move it could make (run_command, WhatsApp) keeps its own
    # gate. Asking "posso continuar?" between every iteration would defeat the
    # feature, which exists precisely to stop the turn ending on a promise.
    IntentType.DIRECT_RESPONSE: "auto",
}


def handoff_requires_confirmation(intent: IntentType) -> bool:
    """True when a handoff to ``intent`` must not auto-execute.

    Unknown targets default to "confirm" (fail safe).
    """
    return HANDOFF_EXECUTION_POLICY.get(intent, "confirm") == "confirm"


def handoff_defers_final(handoffs: "list[object]") -> bool:
    """A resposta que CARREGA um handoff pode ser marcada ``is_final``?

    Não pode — para nenhum alvo. O desktop dá ``break`` no primeiro ``is_final``
    e derruba o stream; o servidor vê "ExecuteTask cancelled by client" e o step
    do handoff, já em voo, morre junto. É o fechamento do stream que mata o
    step, e isso independe de a skill atuar no cliente ou no neo-core.

    Existe como função nomeada porque a versão anterior desta regra era um
    ``any(h.target_intent == MUSIC ...)`` inline: música tinha sido consertada,
    home_assistant ficou de fora pelo motivo errado ("atua no neo-core, então
    pode ser final"), e TODO comando de casa vindo por handoff era cancelado
    0 ms depois de começar — enquanto o cérebro anunciava sucesso.
    """
    return bool(handoffs)
