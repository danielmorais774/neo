"""Sinal de vida de um turno: separa "trabalhando devagar" de "travado".

Um prazo de relógio (N segundos desde o início) não distingue as duas coisas. Na
sessão c4f3f7de (2026-09-13) ele matou um ffmpeg com 89% do vídeo já decodificado
e derrubou dois turnos do desktop que ainda estavam produzindo.

Aqui quem faz o trabalho de verdade avisa que andou (`touch_progress()`), e quem
vigia (o keepalive do gRPC) só atesta vida enquanto o último avanço for recente.
O objeto viaja por ContextVar: o handler do gRPC o instala antes de rodar o turno
e as camadas de baixo (sandbox, stream do LLM) avisam sem recebê-lo por parâmetro.
Fora de um turno (jobs em background, scheduler) não há objeto instalado e
`touch_progress()` não faz nada.
"""

from __future__ import annotations

import time
from contextvars import ContextVar, Token
from typing import Callable


class TurnLiveness:
    """Momento do último avanço observável de um turno."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last_progress = clock()

    def touch(self) -> None:
        self._last_progress = self._clock()

    def idle_seconds(self) -> float:
        return self._clock() - self._last_progress


_current: ContextVar[TurnLiveness | None] = ContextVar("neo_turn_liveness", default=None)


def bind_liveness(liveness: TurnLiveness) -> Token:
    """Instala ``liveness`` no contexto atual (e nas tasks criadas a partir dele)."""
    return _current.set(liveness)


def current_liveness() -> TurnLiveness | None:
    return _current.get()


def touch_progress() -> None:
    """Registra avanço no turno em curso, se houver um."""
    liveness = _current.get()
    if liveness is not None:
        liveness.touch()
