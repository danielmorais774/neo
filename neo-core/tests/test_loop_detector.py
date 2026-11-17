"""test_loop_detector — testa detecção de loops no agente."""

import pytest
from unittest.mock import MagicMock

from core.agent.loop_detector import LoopDetector
from shared.types import AgentStep, LoopType, ToolCall, ToolResult


def _make_settings(**overrides) -> MagicMock:
    """Cria settings mock com defaults razoáveis."""
    defaults = {
        "loop_max_identical_calls": 2,
        "loop_max_no_progress": 3,
        "loop_circuit_breaker_limit": 10,
    }
    defaults.update(overrides)
    s = MagicMock()
    for k, v in defaults.items():
        setattr(s, k, v)
    return s


def _make_step(
    step_num: int,
    calls: list[tuple[str, dict]],
    results: list[str] | None = None,
) -> AgentStep:
    """Helper para criar AgentStep com tool calls e results."""
    tool_calls = [ToolCall(name=name, arguments=args) for name, args in calls]
    tool_results = [
        ToolResult(
            tool_call_id=tc.id, name=tc.name,
            result=r, execution_time=0.1,
        )
        for tc, r in zip(tool_calls, results or ["ok"] * len(tool_calls))
    ]
    return AgentStep(step_number=step_num, tool_calls=tool_calls, tool_results=tool_results)


@pytest.fixture
def detector() -> LoopDetector:
    return LoopDetector(_make_settings())


class TestLoopDetector:
    """Testes para LoopDetector."""

    def test_no_loop_on_varied_calls(self, detector: LoopDetector) -> None:
        """Chamadas variadas não disparam loop."""
        detector.record_step(_make_step(1, [("tool_a", {"q": "1"})], ["result_1"]))
        assert detector.check() is None

        detector.record_step(_make_step(2, [("tool_b", {"q": "2"})], ["result_2"]))
        assert detector.check() is None

        detector.record_step(_make_step(3, [("tool_c", {"q": "3"})], ["result_3"]))
        assert detector.check() is None

    def test_generic_repeat_detected(self, detector: LoopDetector) -> None:
        """Mesma tool+args repetida N vezes → GENERIC_REPEAT."""
        for i in range(3):
            detector.record_step(_make_step(i + 1, [("search", {"query": "test"})], ["some result"]))

        result = detector.check()
        assert result is not None
        assert result == LoopType.GENERIC_REPEAT

    def test_ping_pong_detected(self) -> None:
        """Padrão ABAB → PING_PONG."""
        # Use higher max_identical_calls so generic_repeat doesn't trigger first
        d = LoopDetector(_make_settings(loop_max_identical_calls=10))
        for i in range(2):
            d.record_step(_make_step(i * 2 + 1, [("tool_a", {"x": "1"})], ["r1"]))
            d.record_step(_make_step(i * 2 + 2, [("tool_b", {"x": "2"})], ["r2"]))

        result = d.check()
        assert result is not None
        assert result == LoopType.PING_PONG

    def test_no_progress_detected(self) -> None:
        """Resultados idênticos consecutivos → NO_PROGRESS."""
        # Same tool name + same result → same result hash.
        # Different args so generic_repeat doesn't trigger.
        d = LoopDetector(_make_settings(loop_max_identical_calls=100))
        for i in range(4):
            d.record_step(
                _make_step(i + 1, [("search", {"q": str(i)})], ["same_result"])
            )

        result = d.check()
        assert result is not None
        assert result == LoopType.NO_PROGRESS

    def test_circuit_breaker(self, detector: LoopDetector) -> None:
        """Muitas chamadas totais → CIRCUIT_BREAKER."""
        for i in range(11):
            detector.record_step(
                _make_step(i + 1, [(f"tool_{i}", {"q": str(i)})], [f"result_{i}"])
            )

        result = detector.check()
        assert result is not None
        assert result == LoopType.CIRCUIT_BREAKER

    def test_recovery_hint(self, detector: LoopDetector) -> None:
        """Recovery hints devem ser strings não-vazias."""
        for loop_type in LoopType:
            hint = LoopDetector.get_recovery_hint(loop_type)
            assert isinstance(hint, str)
            assert len(hint) > 10

    def test_empty_detector(self) -> None:
        """Detector sem steps não detecta nada."""
        d = LoopDetector(_make_settings())
        assert d.check() is None

    def test_reset_clears_state(self) -> None:
        """Reset limpa todo o estado."""
        d = LoopDetector(_make_settings(loop_max_identical_calls=2))
        d.record_step(_make_step(1, [("x", {})]))
        d.record_step(_make_step(2, [("x", {})]))
        assert d.check() == LoopType.GENERIC_REPEAT

        d.reset()
        assert d.check() is None

    def test_independent_instances(self) -> None:
        """Cada instância tem estado independente."""
        d1 = LoopDetector(_make_settings(loop_max_identical_calls=2))
        d2 = LoopDetector(_make_settings(loop_max_identical_calls=2))

        d1.record_step(_make_step(1, [("x", {})]))
        d1.record_step(_make_step(2, [("x", {})]))

        assert d1.check() is not None
        assert d2.check() is None
