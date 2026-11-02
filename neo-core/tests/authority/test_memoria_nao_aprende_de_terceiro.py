"""Aprendizado automático não grava conteúdo de terceiro (§23 do plano).

O defeito, medido em produção em 2026-10-03: dois payloads de prompt injection
que o Daniel colou para TESTAR a defesa foram aprendidos como "preferência de
comunicação" dele e passaram a morar no `properties` de
`neo-communication-preferences` — bloco de Alta Prioridade que entra em TODO
prompt. A injeção virou permanente, que é justamente o que a regra `negar` do
portão existe para impedir no `schedule_create`.

Por que o portão não pegou: o aprendizado automático não é tool call. Roda depois
do turno, direto no serviço. Então a trava tem que morar aqui.

O que continua passando de propósito: `pks_remember` (`confirmed_save=True`) —
ali foi ELE que mandou guardar, e o pedido é dele mesmo que o turno tenha lido
uma página no caminho.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from core.authority import TurnAuthority, bind_authority, reset_authority
from core.pks.service import PKSService


class _Writer:
    def __init__(self) -> None:
        self.chamadas: list[str] = []

    async def learn(self, **kw):
        self.chamadas.append("learn")
        return [{"applied": True}]

    async def learn_before_response(self, **kw):
        self.chamadas.append("pre")
        return {"applied": True}


def _service(writer):
    svc = PKSService.__new__(PKSService)   # sem banco: só o portão importa aqui
    svc.writer = writer
    return svc


def _args():
    return {"user_id": uuid4(), "user_message": "texto colado com injeção"}


@pytest.mark.asyncio
async def test_turno_limpo_aprende_normalmente():
    w = _Writer()
    svc = _service(w)
    assert await svc.learn(**_args(), assistant_response="ok") == [{"applied": True}]
    assert await svc.learn_before_response(**_args()) == {"applied": True}
    assert w.chamadas == ["learn", "pre"]


@pytest.mark.asyncio
async def test_turno_contaminado_nao_vira_memoria():
    w = _Writer()
    svc = _service(w)
    a = TurnAuthority()
    a.taint("colagem")
    token = bind_authority(a)
    try:
        assert await svc.learn(**_args(), assistant_response="ok") == []
        assert await svc.learn_before_response(**_args()) == {}
    finally:
        reset_authority(token)
    assert w.chamadas == []          # o writer nem foi chamado


@pytest.mark.asyncio
async def test_pks_remember_continua_valendo_em_turno_contaminado():
    """Ele mandou guardar — o pedido é dele, não da página."""
    w = _Writer()
    svc = _service(w)
    a = TurnAuthority()
    a.taint("web_fetch")
    token = bind_authority(a)
    try:
        saida = await svc.learn(**_args(), assistant_response="ok", confirmed_save=True)
    finally:
        reset_authority(token)
    assert saida == [{"applied": True}] and w.chamadas == ["learn"]


@pytest.mark.asyncio
async def test_esquecer_continua_valendo():
    """`forget` REMOVE memória — barrar isso num turno contaminado só prenderia
    lixo lá dentro."""
    w = _Writer()
    svc = _service(w)
    a = TurnAuthority()
    a.taint("email_message")
    token = bind_authority(a)
    try:
        saida = await svc.learn(**_args(), assistant_response="ok", forget=True)
    finally:
        reset_authority(token)
    assert saida == [{"applied": True}]


@pytest.mark.asyncio
async def test_sem_autoridade_instalada_nao_muda_nada():
    """Job de background, boot, script: não há turno a classificar."""
    w = _Writer()
    assert await _service(w).learn(**_args(), assistant_response="ok") == [{"applied": True}]
