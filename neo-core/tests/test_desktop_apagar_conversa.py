"""Apagar a conversa no histórico do desktop (Conversas → botão direito → Apagar).

O DELETE antigo só limpava a cópia em memória: a conversa voltava no refresh.
Agora apaga no banco, e recusa (409) quando ainda há trabalho que vai escrever na
sessão — senão a continuação recriaria a conversa com o mesmo id.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

import api.routes.desktop as rotas


class _DB:
    def __init__(self, *, jobs=0, agendas=0, mensagens=3):
        self.aberto = {"jobs": jobs, "agendas": agendas}
        self.mensagens = mensagens
        self.apagadas: list = []

    async def count_open_work_for_session(self, session_id):
        return self.aberto

    async def delete_conversation_session(self, session_id):
        self.apagadas.append(session_id)
        return self.mensagens


class _Service:
    def __init__(self):
        self.limpas: list = []

    def clear_session(self, session_id):
        self.limpas.append(session_id)
        return False  # nada em memória: não pode virar 404


def _dono(sessao):
    async def _f(*a, **k):
        return sessao

    return _f


@pytest.mark.asyncio
async def test_apaga_no_banco_e_na_memoria(monkeypatch):
    monkeypatch.setattr(rotas, "_sessao_do_dono", _dono(SimpleNamespace(id="x")))
    sid, db, service = uuid4(), _DB(mensagens=7), _Service()

    out = await rotas.delete_session(sid, http_request=None, service=service, db=db, principal=None)

    assert out == {"deleted": True, "session_id": str(sid), "messages": 7}
    assert db.apagadas == [sid]
    assert service.limpas == [sid]


@pytest.mark.asyncio
async def test_sessao_que_nao_existe_e_404(monkeypatch):
    monkeypatch.setattr(rotas, "_sessao_do_dono", _dono(None))
    db = _DB()

    with pytest.raises(HTTPException) as erro:
        await rotas.delete_session(uuid4(), http_request=None, service=_Service(), db=db, principal=None)

    assert erro.value.status_code == 404
    assert db.apagadas == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "jobs, agendas, trecho",
    [
        (1, 0, "tem 1 tarefa em andamento."),
        (3, 0, "tem 3 tarefas em andamento."),
        (0, 2, "tem 2 agendas ligadas."),
        (1, 1, "tem 1 tarefa em andamento e 1 agenda ligada."),
    ],
)
async def test_trabalho_aberto_na_sessao_barra_o_apagar(monkeypatch, jobs, agendas, trecho):
    monkeypatch.setattr(rotas, "_sessao_do_dono", _dono(SimpleNamespace(id="x")))
    db = _DB(jobs=jobs, agendas=agendas)

    with pytest.raises(HTTPException) as erro:
        await rotas.delete_session(uuid4(), http_request=None, service=_Service(), db=db, principal=None)

    assert erro.value.status_code == 409
    assert trecho in erro.value.detail
    assert db.apagadas == []
