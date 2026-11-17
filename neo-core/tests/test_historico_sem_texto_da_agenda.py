"""Reabrir a conversa não desenha o texto da agenda como mensagem do Daniel.

session-bd44cc03: a continuação de um job é um turno agendado, e o texto que o
turno recebe ("[Continuação automática — não é mensagem do usuário…]", 6.000
caracteres) ficava salvo como `user`. Ao vivo o desktop nunca o mostrava; o
histórico, sim.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

import api.routes.desktop as rotas


def _msg(role: str, content: str, **meta) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), role=role, content=content, created_at=datetime.utcnow(),
        extra_metadata=meta or None, mission_id=None,
    )


class _DB:
    def __init__(self, mensagens):
        self.mensagens = mensagens

    async def fetch_session_messages(self, **kwargs):
        return self.mensagens


@pytest.mark.asyncio
async def test_historico_esconde_o_texto_que_a_agenda_mandou_ao_turno(monkeypatch):
    async def dono(*a, **k):
        return None

    monkeypatch.setattr(rotas, "_sessao_do_dono", dono)
    mensagens = [
        _msg("user", "roda de novo para ter certeza"),
        _msg("user", "[Continuação automática — não é mensagem do usuário.] …",
             source="schedule", client="neo-scheduler", unattended=True),
        _msg("assistant", "Concluí a verificação.", tool_trace=[]),
    ]
    out = await rotas.get_persistent_session_history(
        uuid4(), http_request=None, limit=100, db=_DB(mensagens), principal=None
    )
    assert [m["content"] for m in out["messages"]] == [
        "roda de novo para ter certeza",
        "Concluí a verificação.",
    ]
