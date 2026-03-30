"""Loop detector — detecta padrões repetitivos no agente.

Implementa 4 detectores inspirados no OpenClaw:
  1. generic_repeat  — mesma tool call repetida N vezes
  2. ping_pong       — alternância A→B→A→B entre tool calls
  3. no_progress     — iterações sem conteúdo novo
  4. circuit_breaker — limite global de tool calls atingido
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from typing import TYPE_CHECKING

from shared.types import LoopType

if TYPE_CHECKING:
    from shared.config import Settings
    from shared.types import AgentStep

logger = logging.getLogger(__name__)


def _call_fingerprint(tool_name: str, arguments: dict) -> str:
    """Gera fingerprint determinístico para uma tool call (name + args)."""
    raw = f"{tool_name}::{json.dumps(arguments, sort_keys=True, default=str)}"
    return hashlib.md5(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Recovery hints — dicas para o LLM sair do loop
# ---------------------------------------------------------------------------

_RECOVERY_HINTS: dict[LoopType, str] = {
    LoopType.GENERIC_REPEAT: (
        "ATENÇÃO: Você está repetindo a mesma ação com os mesmos parâmetros. "
        "Isso não vai produzir resultados diferentes. "
        "Mude sua abordagem ou forneça uma resposta final ao usuário."
    ),
    LoopType.PING_PONG: (
        "ATENÇÃO: Você está alternando entre duas ações sem avançar. "
        "Pare, analise os resultados que já tem e tome uma decisão final."
    ),
    LoopType.NO_PROGRESS: (
        "ATENÇÃO: As últimas iterações não produziram progresso real. "
        "Resuma o que já conseguiu e dê uma resposta ao usuário, "
        "mesmo que parcial."
    ),
    LoopType.CIRCUIT_BREAKER: (
        "ATENÇÃO: Limite de operações atingido. "
        "Finalize imediatamente com o melhor resultado que tem."
    ),
}


class LoopDetector:
    """Detecta padrões de loop durante execução do agente.

    Mantém estado interno e é resetado a cada nova execução.
    """

    def __init__(self, settings: Settings) -> None:
        self._max_identical = settings.loop_max_identical_calls
        self._max_no_progress = settings.loop_max_no_progress
        self._circuit_limit = settings.loop_circuit_breaker_limit

        # Estado acumulado durante uma execução
        self._call_history: list[str] = []          # fingerprints em ordem
        self._call_counts: Counter[str] = Counter()  # contagem por fingerprint
        self._total_calls: int = 0
        self._result_hashes: list[str] = []          # hash dos resultados

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Limpa estado — chamar no início de cada execução."""
        self._call_history.clear()
        self._call_counts.clear()
        self._total_calls = 0
        self._result_hashes.clear()

    def record_step(self, step: AgentStep) -> None:
        """Registra um step para análise.

        Deve ser chamado APÓS a execução das tools do step.
        """
        for tc in step.tool_calls:
            fp = _call_fingerprint(tc.name, tc.arguments)
            self._call_history.append(fp)
            self._call_counts[fp] += 1
            self._total_calls += 1

        # Hash dos resultados para detectar falta de progresso
        results_raw = json.dumps(
            [{"name": r.name, "result": str(r.result)[:200]} for r in step.tool_results],
            sort_keys=True,
            default=str,
        )
        self._result_hashes.append(hashlib.md5(results_raw.encode()).hexdigest())

    def check(self) -> LoopType | None:
        """Roda todos os detectores. Retorna o primeiro loop encontrado ou None."""
        detectors = [
            self._detect_circuit_breaker,
            self._detect_generic_repeat,
            self._detect_ping_pong,
            self._detect_no_progress,
        ]
        for detector in detectors:
            result = detector()
            if result is not None:
                logger.warning(f"Loop detectado: {result.value}")
                return result
        return None

    @staticmethod
    def get_recovery_hint(loop_type: LoopType) -> str:
        """Retorna dica contextual para o LLM sair do loop."""
        return _RECOVERY_HINTS.get(loop_type, "Tente uma abordagem diferente.")

    # ------------------------------------------------------------------
    # Detectores individuais
    # ------------------------------------------------------------------

    def _detect_generic_repeat(self) -> LoopType | None:
        """Detecta se a mesma tool call (name + args) foi repetida demais."""
        for fp, count in self._call_counts.items():
            if count >= self._max_identical:
                return LoopType.GENERIC_REPEAT
        return None

    def _detect_ping_pong(self) -> LoopType | None:
        """Detecta alternância A→B→A→B nas últimas 4 calls."""
        if len(self._call_history) < 4:
            return None
        last_four = self._call_history[-4:]
        # Padrão: A B A B (posições 0==2 e 1==3, mas 0!=1)
        if (
            last_four[0] == last_four[2]
            and last_four[1] == last_four[3]
            and last_four[0] != last_four[1]
        ):
            return LoopType.PING_PONG
        return None

    def _detect_no_progress(self) -> LoopType | None:
        """Detecta N iterações consecutivas com resultados idênticos."""
        if len(self._result_hashes) < self._max_no_progress:
            return None
        recent = self._result_hashes[-self._max_no_progress:]
        if len(set(recent)) == 1:
            return LoopType.NO_PROGRESS
        return None

    def _detect_circuit_breaker(self) -> LoopType | None:
        """Detecta se o total de tool calls excedeu o limite global."""
        if self._total_calls >= self._circuit_limit:
            return LoopType.CIRCUIT_BREAKER
        return None
