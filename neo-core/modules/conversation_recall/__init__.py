"""Conversation recall module — busca no arquivo de conversas passadas.

Ver docs/conversation-recall-plan.md.
"""

from modules.conversation_recall.executor import (
    CURSOR_TTL_TURNS,
    ConversationRecallExecutor,
)
from modules.conversation_recall.render import render_recall_block, render_recall_summary
from modules.conversation_recall.tools import CONVERSATION_RECALL_TOOLS

CONVERSATION_RECALL_TOOL_NAMES = frozenset(t.name for t in CONVERSATION_RECALL_TOOLS)

__all__ = [
    "CONVERSATION_RECALL_TOOLS",
    "CONVERSATION_RECALL_TOOL_NAMES",
    "CURSOR_TTL_TURNS",
    "ConversationRecallExecutor",
    "render_recall_block",
    "render_recall_summary",
]
