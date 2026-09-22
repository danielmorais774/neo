"""Com os defaults, a agenda não existe.

Mesma disciplina do Modo Birra: o neo-core roda com ``--reload`` e salvar é
deploy, então a suíte precisa provar que sem mexer no ``.env`` nada dispara.
"""

from __future__ import annotations

import pytest

from shared.config import Settings


def test_flag_desligada_por_default() -> None:
    assert Settings().neo_scheduler_enabled is False


@pytest.mark.asyncio
async def test_tick_desligado_nao_sobe_o_laco() -> None:
    from core.scheduler import SchedulerTick

    tick = SchedulerTick(db=None, service=None, enabled=False)
    await tick.start()
    assert tick._task is None


def test_prefixo_schedule_fica_fora_do_roteamento_direto() -> None:
    """Quem agenda é o cognitivo, com a frase inteira na mão."""
    from core.router.intent_router import server_tool_can_run

    assert server_tool_can_run("schedule_create") is False
