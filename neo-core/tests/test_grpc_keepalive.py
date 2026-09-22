"""with_keepalive — turno longo não pode parecer travado, e turno travado não pode parecer vivo.

Sessão c4f3f7de: o desktop desistia aos 300s de relógio com o turno ainda
produzindo (ffmpeg no sandbox). Agora o handler intercala keepalives enquanto o
turno progride e fica mudo quando o progresso para.
"""

import asyncio

import pytest

from core.liveness import TurnLiveness
from grpc_server import with_keepalive


async def _collect(agen):
    return [item async for item in agen]


class _Stalled(TurnLiveness):
    def idle_seconds(self) -> float:
        return 10_000.0


@pytest.mark.asyncio
async def test_passes_responses_through_in_order():
    async def turn():
        yield "a"
        yield "b"

    assert await _collect(with_keepalive(turn(), TurnLiveness(), interval=1)) == ["a", "b"]


@pytest.mark.asyncio
async def test_beats_while_a_slow_step_is_progressing():
    async def turn():
        await asyncio.sleep(0.08)
        yield "done"

    out = await _collect(with_keepalive(turn(), TurnLiveness(), interval=0.01))
    assert out[-1] == "done"
    assert out.count(None) >= 3


@pytest.mark.asyncio
async def test_goes_silent_when_the_turn_stopped_progressing():
    async def turn():
        await asyncio.sleep(0.08)
        yield "done"

    out = await _collect(with_keepalive(turn(), _Stalled(), interval=0.01, stall_window=240))
    assert out == ["done"]


@pytest.mark.asyncio
async def test_turn_exception_reaches_the_handler():
    async def turn():
        yield "a"
        raise RuntimeError("boom")

    agen = with_keepalive(turn(), TurnLiveness(), interval=1)
    assert await agen.__anext__() == "a"
    with pytest.raises(RuntimeError, match="boom"):
        await agen.__anext__()


@pytest.mark.asyncio
async def test_client_leaving_cancels_the_turn():
    cancelled = asyncio.Event()

    async def turn():
        yield "a"
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        yield "never"

    agen = with_keepalive(turn(), TurnLiveness(), interval=1)
    assert await agen.__anext__() == "a"
    await asyncio.sleep(0)
    await agen.aclose()
    await asyncio.wait_for(cancelled.wait(), timeout=1)
