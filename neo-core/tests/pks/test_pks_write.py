"""Testes da escrita sob demanda no PKS (docs/pks-write-on-demand-plan.md).

Cobre: (1) o modo confirmado do PKSMemoryPlanner injeta o override que faz o
subagente ESTRUTURAR sem re-julgar; (2) o echo do PKSWriteExecutor reflete o
que foi (ou não foi) salvo.
"""

import asyncio

from core.pks.memory_planner import PKSMemoryPlanner
from modules.pks_write.executor import _format_echo, _format_forget_echo


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self._content = content

    def get_content(self) -> str:
        return self._content


class _FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.last_system: str | None = None
        self.last_model: str | None = None

    async def complete(self, *, messages, temperature, max_tokens, model=None, tools=None):
        # messages[0] é o system prompt do planner.
        self.last_system = messages[0].content
        self.last_model = model
        return _FakeResponse(
            '{"operations": [{"op": "upsert_entity", "confidence": 0.95, '
            '"reason": "confirmado pelo usuário", '
            '"entity": {"name": "Suco de uva pré-treino", "type": "preference"}}]}'
        )


class _FakeRouter:
    def __init__(self, provider: _FakeProvider) -> None:
        self._provider = provider

    def get_provider(self, name=None):
        return self._provider

    def get_provider_for_model(self, model=None):
        return self._provider


def _run(coro):
    # asyncio.run, e não get_event_loop().run_until_complete: o segundo só
    # funciona enquanto NENHUM teste async tiver rodado antes neste processo
    # (pytest-asyncio fecha o loop ao terminar, e aí get_event_loop levanta
    # "There is no current event loop"). Estes testes são síncronos, então
    # dependiam da ordem de coleta para passar.
    return asyncio.run(coro)


def test_planner_confirmed_mode_injects_override_and_structures():
    provider = _FakeProvider()
    planner = PKSMemoryPlanner(_FakeRouter(provider))

    ops = _run(
        planner.plan(
            user_message="salva isto na memória",
            assistant_response="Pré-treino sem cafeína: suco de uva integral + água com gás.",
            pks_context="",
            recent_history="",
            confirmed_save=True,
        )
    )

    assert "MODO CONFIRMADO" in (provider.last_system or "")
    # Modelo explícito e válido (não o 'deepseek-chat' aposentado) — regressão
    # do bug que fazia toda gravação falhar com 400 (session-77a77b00).
    assert provider.last_model == "deepseek-v4-flash"
    assert len(ops) == 1
    assert ops[0].entity["type"] == "preference"


class _FakeForgetProvider(_FakeProvider):
    async def complete(self, *, messages, temperature, max_tokens, model=None, tools=None):
        self.last_system = messages[0].content
        self.last_model = model
        return _FakeResponse(
            '{"operations": [{"op": "soft_delete_entity", "confidence": 0.95, '
            '"reason": "usuário pediu para esquecer", '
            '"entity": {"name": "Bebida pré-treino sem cafeína preferida", "type": "preference"}}]}'
        )


def test_planner_forget_mode_injects_override_and_emits_soft_delete():
    provider = _FakeForgetProvider()
    planner = PKSMemoryPlanner(_FakeRouter(provider))

    ops = _run(
        planner.plan(
            user_message="esquece isto da memória: bebida pré-treino",
            assistant_response="bebida pré-treino sem cafeína",
            pks_context="### Bebida pré-treino sem cafeína preferida [preference]",
            recent_history="",
            forget=True,
        )
    )

    assert "MODO ESQUECER" in (provider.last_system or "")
    # No modo esquecer o override de salvar NÃO entra.
    assert "MODO CONFIRMADO" not in (provider.last_system or "")
    assert len(ops) == 1
    assert ops[0].op == "soft_delete_entity"


def test_format_forget_echo_reports_removed_and_not_found():
    removed = _format_forget_echo(
        "bebida pré-treino",
        [{"op": "soft_delete_entity", "entity_id": "x", "name": "Bebida pré-treino sem cafeína preferida"}],
    )
    assert "Removi da sua memória" in removed
    assert "Bebida pré-treino sem cafeína preferida" in removed

    nada = _format_forget_echo("coisa inexistente", [])
    assert "NÃO encontrei nada" in nada
    assert "não afirme que esqueceu" in nada.lower()


def test_planner_default_mode_has_no_override():
    provider = _FakeProvider()
    planner = PKSMemoryPlanner(_FakeRouter(provider))

    _run(
        planner.plan(
            user_message="oi",
            assistant_response="e aí",
            pks_context="",
            recent_history="",
        )
    )

    assert "MODO CONFIRMADO" not in (provider.last_system or "")


def test_format_echo_summarizes_saved_entities():
    applied = [
        {"op": "upsert_entity", "name": "Suco de uva pré-treino", "type": "preference"},
        {"op": "upsert_field", "key": "receita"},
    ]
    echo = _format_echo("Suco de uva integral + água com gás", applied)
    assert "Salvei na sua memória" in echo
    assert "Suco de uva pré-treino (preference)" in echo


def test_format_echo_reports_nothing_saved_when_empty():
    echo = _format_echo("algo", [])
    assert "NÃO foi possível" in echo
    assert "não afirme que salvou" in echo.lower()


def test_salvage_operations_recovers_complete_ops_from_truncated_json():
    """JSON cortado no meio (modelo estourou max_tokens): salva o que completou
    em vez de perder tudo — regressão da session-77a77b00."""
    truncated = (
        '{"operations": ['
        '{"op": "upsert_entity", "entity": {"name": "A", "type": "preference"}}, '
        '{"op": "upsert_field", "field": {"key": "recipe"}}, '
        '{"op": "upsert_entity", "entity": {"name": "Ident'  # cortado
    )
    ops = PKSMemoryPlanner._salvage_operations(truncated)
    assert [o["op"] for o in ops] == ["upsert_entity", "upsert_field"]


def test_parse_falls_back_to_salvage_on_truncation():
    planner = object.__new__(PKSMemoryPlanner)
    truncated = (
        '{"operations": ['
        '{"op": "upsert_entity", "confidence": 0.95, "reason": "x", '
        '"entity": {"name": "Suco de uva", "type": "preference"}}, '
        '{"op": "upsert_field", "confidence"'  # cortado
    )
    ops = planner._parse(truncated, max_operations=40)
    assert len(ops) == 1
    assert ops[0].entity["name"] == "Suco de uva"
