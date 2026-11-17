"""Caixa de email do Neo — IMAP para ler, SMTP para enviar.

O endereço e os servidores vivem no cofre (credencial ``kind="email"``), não em
env: é assim que o painel cadastra e que o ``vault_list`` deixa o cérebro
descobrir que existe um email disponível.

Ler e enviar; **não** apagar, mover nem responder-em-thread. O envio sai sempre
em nome do próprio Neo e fica atrás de ``NEO_EMAIL_SEND_ENABLED`` — por isso as
tools de envio são uma lista separada (ver core/mailbox/tools.py).
"""

from core.mailbox.client import (
    MailboxError,
    get_attachment,
    get_message,
    list_inbox,
    send_message,
)
from core.mailbox.tools import (
    EMAIL_SEND_TOOL_NAMES,
    EMAIL_SEND_TOOLS,
    EMAIL_TOOL_NAMES,
    EMAIL_TOOLS,
    MailboxToolExecutor,
)

__all__ = [
    "MailboxError",
    "list_inbox",
    "get_message",
    "get_attachment",
    "send_message",
    "EMAIL_TOOLS",
    "EMAIL_TOOL_NAMES",
    "EMAIL_SEND_TOOLS",
    "EMAIL_SEND_TOOL_NAMES",
    "MailboxToolExecutor",
]
