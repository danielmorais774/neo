"""Retomada depois da aprovação (F3c) — as guardas, que é o que importa aqui."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from core.release.resume import _esquecer, build_resumer
from db.models.release_request import (
    STATUS_APROVADA,
    STATUS_PENDENTE,
    STATUS_RECUSADA,
    ReleaseRequest,
)


class _AIFalso:
    def __init__(self, final: str = "frete calculado: R$ 21,90") -> None:
        self.turnos: list[dict] = []
        self._final = final

    def process_user_input(self, **kwargs):
        self.turnos.append(kwargs)
        final = self._final

        class _Resp:
            is_final = True
            message = final
            session_id = kwargs.get("session_id")
            metadata: dict = {}

        async def _gen():
            yield _Resp()

        return _gen()


def _row(**kw) -> ReleaseRequest:
    base = dict(
        user_id=uuid4(),
        session_id=uuid4(),
        origem="admin",
        campos=["pks:cep"],
        destino="loja.com.br",
        destino_tipo="dominio",
        finalidade="calcular frete",
        status=STATUS_APROVADA,
    )
    base.update(kw)
    row = ReleaseRequest(**base)
    return row


@pytest.mark.asyncio
async def test_aprovada_dispara_um_turno():
    ai = _AIFalso()
    publicados: list[str] = []

    async def announce(row, texto):
        publicados.append(texto)

    resume = build_resumer(get_ai_service=lambda: ai, announce=announce)
    row = _row()
    try:
        assert await resume(row) is True
        await asyncio.sleep(0.05)  # a task é fire-and-forget
        assert len(ai.turnos) == 1
        assert publicados == ["frete calculado: R$ 21,90"]
    finally:
        _esquecer(row.id)


@pytest.mark.asyncio
async def test_o_turno_nasce_como_pedido_seu_e_sem_tool_de_cliente():
    ai = _AIFalso()
    resume = build_resumer(get_ai_service=lambda: ai)
    row = _row()
    try:
        await resume(row)
        await asyncio.sleep(0.05)
        meta = ai.turnos[0]["metadata"]
        assert meta["principal"] == "admin"  # você aprovou, a intenção é sua
        assert meta["unattended"] is True    # nasceu de um clique, não do terminal
        assert meta["source"] == "release"
        assert meta["release_id"] == str(row.id)
    finally:
        _esquecer(row.id)


@pytest.mark.asyncio
async def test_a_instrucao_proibe_pedir_a_mesma_liberacao():
    """Sem isto, um turno que falha vira um card atrás do outro."""
    ai = _AIFalso()
    resume = build_resumer(get_ai_service=lambda: ai)
    row = _row()
    try:
        await resume(row)
        await asyncio.sleep(0.05)
        texto = ai.turnos[0]["user_message"]
        assert "NÃO chame solicitar_dado" in texto
        assert "cep" in texto and "loja.com.br" in texto
        assert "«pks:campo»" in texto
    finally:
        _esquecer(row.id)


@pytest.mark.asyncio
async def test_recusada_nao_retoma():
    ai = _AIFalso()
    resume = build_resumer(get_ai_service=lambda: ai)
    row = _row(status=STATUS_RECUSADA)
    assert await resume(row) is False
    assert ai.turnos == []


@pytest.mark.asyncio
async def test_pendente_nao_retoma():
    ai = _AIFalso()
    resume = build_resumer(get_ai_service=lambda: ai)
    assert await resume(_row(status=STATUS_PENDENTE)) is False
    assert ai.turnos == []


@pytest.mark.asyncio
async def test_sem_sessao_nao_ha_o_que_continuar():
    ai = _AIFalso()
    resume = build_resumer(get_ai_service=lambda: ai)
    assert await resume(_row(session_id=None)) is False
    assert ai.turnos == []


@pytest.mark.asyncio
async def test_so_retoma_uma_vez_por_liberacao():
    ai = _AIFalso()
    resume = build_resumer(get_ai_service=lambda: ai)
    row = _row()
    try:
        assert await resume(row) is True
        assert await resume(row) is False
        await asyncio.sleep(0.05)
        assert len(ai.turnos) == 1
    finally:
        _esquecer(row.id)


@pytest.mark.asyncio
async def test_sem_ai_service_nao_quebra():
    resume = build_resumer(get_ai_service=lambda: None)
    assert await resume(_row()) is False


@pytest.mark.asyncio
async def test_turno_que_quebra_nao_derruba_nada():
    class _Explode:
        def process_user_input(self, **kwargs):
            async def _gen():
                raise RuntimeError("provedor caiu")
                yield  # pragma: no cover

            return _gen()

    resume = build_resumer(get_ai_service=lambda: _Explode())
    row = _row()
    try:
        assert await resume(row) is True  # a decisão não depende do turno
        await asyncio.sleep(0.05)
    finally:
        _esquecer(row.id)
