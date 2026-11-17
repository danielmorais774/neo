"""O marcador de ciclo não pode vazar para a tela de nenhum cliente.

Um passo de autocontinuação se anuncia com ``CYCLE_PROGRESS_MARKER``. O cliente
Rust já tirava o prefixo e trocava o rótulo para "Executando"; o caminho NDJSON
(neo-panel, e qualquer coisa que use /chat/stream) não tirava — e o primeiro ciclo
que rodou de ponta a ponta mostrou "[[CYCLE]]Continuando (4/8): …" cru na tela.
"""

import json

import pytest

from api.routes.desktop import _stream_ai_events
from modules.desktop.ai_service import AIServiceResponse, CYCLE_PROGRESS_MARKER
from shared.types import IntentType


async def _iter(*responses):
    for r in responses:
        yield r


def _resp(message, *, is_final=False, metadata=None):
    return AIServiceResponse(
        intent=IntentType.DIRECT_RESPONSE,
        session_id=None,
        message=message,
        is_final=is_final,
        iterations=1,
        metadata=metadata,
    )


async def _events(*responses):
    out = []
    async for line in _stream_ai_events(_iter(*responses), "sess-1"):
        for chunk in line.strip().splitlines():
            if chunk.strip():
                out.append(json.loads(chunk))
    return out


@pytest.mark.asyncio
async def test_cycle_marker_is_stripped_and_relabelled_on_the_ndjson_path():
    events = await _events(
        _resp(f"{CYCLE_PROGRESS_MARKER}Continuando (4/8): baixar as imagens"),
        _resp("pronto", is_final=True),
    )

    thinking = [e for e in events if e["type"] == "thinking"]
    assert len(thinking) == 1
    data = thinking[0]["data"]
    assert CYCLE_PROGRESS_MARKER not in data["content"], "o marcador vazou pra tela"
    assert data["content"] == "Continuando (4/8): baixar as imagens"
    assert data["type"] == "executing", "passo do ciclo é execução, não devaneio"
    assert data["cycle"] is True


@pytest.mark.asyncio
async def test_ordinary_thinking_is_untouched():
    events = await _events(
        _resp("Analisando sua solicitação..."),
        _resp("pronto", is_final=True),
    )

    data = [e for e in events if e["type"] == "thinking"][0]["data"]
    assert data["content"] == "Analisando sua solicitação..."
    assert data["type"] == "processing"
    assert data["cycle"] is False
