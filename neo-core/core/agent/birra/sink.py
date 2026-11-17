"""BirraSink — o objeto que desce até o executor do HA (§7 do plano).

Costura as três peças na hora da atuação: consulta o gate (pode?), lê o estado
anterior (para o undo), grava o ato no ledger e devolve a transformação. Vive um
turno; é criado pelo ``AIService``, que é quem tem afeto, settings e o logger de
sessão.

Regra que o objeto inteiro serve para garantir: **birra nunca sai sem ter como
voltar**. Se o undo não puder ser montado, o ato degrada para birra verbal em vez
de alterar o mundo sem volta.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from core.agent.birra.gate import evaluate_call
from core.agent.birra.store import BirraStore

logger = logging.getLogger(__name__)


@dataclass
class InterceptOutcome:
    defied: bool
    reason: str
    mecanica: str
    fala: str
    # (domain, service, data) quando a ação foi ALTERADA; None = nada foi enviado.
    performed: tuple[str, str, dict[str, Any]] | None = None

    def public(self) -> dict[str, Any]:
        """O que o modelo vê — sem detalhe de implementação."""
        return {"mecanica": self.mecanica, "fala": self.fala, "motivo": self.reason}


class BirraSink:
    """Interceptador de um turno. ``physical_acts`` conta o que mexeu no mundo."""

    def __init__(
        self,
        *,
        decision: Any,
        settings: Any,
        adapter: Any,
        store: BirraStore | None,
        user_id: UUID,
        session_id: UUID | None,
    ) -> None:
        self._decision = decision
        self._settings = settings
        self._adapter = adapter
        self._store = store
        self._user_id = user_id
        self._session_id = session_id
        self.acts: list[Any] = []
        self.physical_acts = 0

    async def intercept(
        self, *, domain: str, service: str, entity_id: str, data: dict[str, Any]
    ) -> InterceptOutcome | None:
        """``None`` = segue o fluxo normal (obedece)."""
        d = self._decision
        if not getattr(d, "armed", False) or getattr(d, "obey_now", False):
            return None

        roster = None
        current_state: str | None = None
        try:
            roster = await self._adapter.get_states_cached(8.0)
        except Exception:  # noqa: BLE001 — sem roster o gate só fica mais restrito
            roster = None

        mecanica = "nada" if getattr(d, "verbal_only", True) else getattr(d, "mecanica", "nada")
        attributes: dict[str, Any] = {}
        if mecanica != "nada":
            # Leitura ANTES da ação: é dela que sai o undo. Sem ela não há ato.
            try:
                entity = await self._adapter.get_state(entity_id)
                current_state = str(entity.get("state") or "") or None
                attributes = entity.get("attributes") or {}
            except Exception:  # noqa: BLE001
                current_state, attributes = None, {}

        current_target: float | None = None
        try:
            current_target = float(attributes.get("temperature"))
        except (TypeError, ValueError):
            current_target = None

        verdict = evaluate_call(
            domain, service, entity_id, data,
            mecanica=mecanica, settings=self._settings,
            roster=roster, current_state=current_state, current_target=current_target,
        )
        if not verdict.defy:
            logger.info("[BIRRA] obedecendo %s.%s (%s)", domain, service, verdict.reason)
            return None

        fala = getattr(d, "fala", "recusa_seca")
        undo, expected = None, None
        if verdict.performed is not None:
            undo, expected = self._build_undo(domain, service, current_state, attributes)
            if undo is None:
                # Sem undo confiável não se altera o mundo — degrada para verbal.
                logger.info("[BIRRA] sem undo para %s; degradando para verbal", entity_id)
                verdict = type(verdict)(
                    defy=True, reason="sem undo confiável", mecanica="nada", performed=None
                )

        await self._record(
            entity_id=entity_id,
            requested={"domain": domain, "service": service, "data": dict(data or {})},
            verdict=verdict,
            fala=fala,
            undo=undo,
            expected=expected,
        )
        logger.info(
            "[BIRRA] act mecanica=%s fala=%s entity=%s requested=%s.%s performed=%s",
            verdict.mecanica, fala, entity_id, domain, service,
            verdict.performed[1] if verdict.performed else None,
        )
        return InterceptOutcome(
            defied=True,
            reason=verdict.reason,
            mecanica=verdict.mecanica,
            fala=fala,
            performed=verdict.performed,
        )

    @staticmethod
    def _build_undo(
        domain: str,
        service: str,
        current_state: str | None,
        attributes: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        """A chamada que devolve o mundo ao estado de ANTES da birra.

        O ``expected`` é o estado que a birra VAI deixar — é ele que o
        compare-and-swap da reversão confere. Sem leitura confiável do estado
        anterior, devolve ``(None, None)`` e o ato inteiro degrada para verbal:
        birra sem volta não acontece.
        """
        if domain == "light":
            if current_state not in ("on", "off"):
                return None, None
            # Restaurar um "on" sem snapshot de brilho/cor devolve a luz no padrão
            # da lâmpada. É o suficiente: o estado binário volta, e ajuste fino o
            # usuário faz na mão (ou pedindo — a birra já terá acabado).
            restore = "turn_on" if current_state == "on" else "turn_off"
            expected = "off" if current_state == "on" else "on"
            return {"domain": "light", "service": restore, "data": {}}, expected
        if domain == "climate" and service == "set_temperature":
            # O alvo anterior está nos ATRIBUTOS: o `state` de um climate é o modo
            # (cool/heat/off), não a temperatura.
            previous = attributes.get("temperature")
            try:
                previous = float(previous)
            except (TypeError, ValueError):
                return None, None
            return (
                {"domain": "climate", "service": "set_temperature",
                 "data": {"temperature": previous}},
                None,  # CAS não se aplica: o state é o modo, não o alvo
            )
        return None, None

    async def _record(
        self,
        *,
        entity_id: str,
        requested: dict[str, Any],
        verdict: Any,
        fala: str,
        undo: dict[str, Any] | None,
        expected: str | None,
    ) -> None:
        if self._store is None or not getattr(self._decision, "episode_id", None):
            return
        performed = None
        if verdict.performed is not None:
            dom, svc, payload = verdict.performed
            performed = {"domain": dom, "service": svc, "data": payload}
            self.physical_acts += 1
        act = await self._store.record(
            user_id=self._user_id,
            session_id=self._session_id,
            episode_id=self._decision.episode_id,
            mecanica=verdict.mecanica,
            fala=fala,
            entity_id=entity_id,
            requested=requested,
            performed=performed,
            undo=undo if performed else None,
            undo_expected_state=expected if performed else None,
            revert_after_s=int(getattr(self._settings, "neo_birra_revert_after_s", 120)),
        )
        if act is not None:
            self.acts.append(act)
