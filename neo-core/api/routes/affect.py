"""Affect API routes — expõe o estado emocional do Neo por usuário para o painel.

GET devolve o estado bruto + pós-decay ao vivo + derivados (bandas, dominante,
humor, postura), para o painel não recalcular regra de negócio. POST /reset é a
válvula de escape admin: volta tudo para os baselines de personalidade.
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from core.pks import PKSService
from shared.config import get_settings

router = APIRouter(prefix="/affect", tags=["affect"])

_pks: PKSService | None = None


def set_pks_service(service: PKSService) -> None:
    global _pks
    _pks = service


@router.get("")
async def get_affect(user_id: str = Query(...)):
    """Estado emocional do Neo com este usuário, com decay aplicado ao vivo."""
    from core.agent.affect import (
        AXES,
        AffectTuning,
        apply_decay,
        band,
        combo_key,
        dominant_axes,
        humor_geral,
        last_pressure_event,
        posture_key,
        POSTURE_TEXT,
    )

    if _pks is None:
        raise HTTPException(status_code=503, detail="PKS service not ready")
    try:
        uid = UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="user_id must be a UUID") from exc

    tuning = AffectTuning.from_settings(get_settings())
    state = await _pks.get_user_affect(user_id=uid)
    live = apply_decay(state, tuning)

    ranked = dominant_axes(live.stats, tuning)
    dominante = None
    if ranked and ranked[0][1] >= tuning.band_medio:
        axis = ranked[0][0]
        dominante = {
            "eixo": axis,
            "banda": band(
                getattr(live.stats, axis), medio=tuning.band_medio, alto=tuning.band_alto
            ).value,
            "combo": combo_key(live.stats, tuning),
        }

    pressure = last_pressure_event(live, tuning)
    postura = None
    if pressure:
        key = posture_key(live.stats)
        postura = {"key": key, "text": POSTURE_TEXT[key], "since": pressure.get("t")}

    return {
        "user_id": user_id,
        "stats_raw": state.stats.as_dict(),
        "stats_live": live.stats.as_dict(),
        "baselines": {axis: tuning.baselines[axis] for axis in AXES},
        "bands": {
            axis: band(
                getattr(live.stats, axis), medio=tuning.band_medio, alto=tuning.band_alto
            ).value
            for axis in AXES
        },
        "dominante": dominante,
        "humor_geral": round(humor_geral(live.stats), 3),
        "postura": postura,
        "last_trigger": live.last_trigger,
        "grudge": live.grudge or {"count": 0},
        "recent": live.recent,
        "updated_at": live.updated_at,
        "enabled": get_settings().neo_affect_enabled,
        "health": _appraiser_health(),
    }


def _appraiser_health() -> dict:
    from core.agent.affect_updater import get_affect_health

    return get_affect_health()


class AffectSetPayload(BaseModel):
    """Ajuste manual. Campos ausentes NÃO são tocados."""

    stats: dict[str, float] | None = None
    grudge_count: int | None = None
    trigger: str | None = None
    reason: str = ""


@router.post("/set")
async def set_affect(payload: AffectSetPayload, user_id: str = Query(...)):
    """Escreve o estado emocional na mão (sliders do painel).

    Existe porque testar humor exigia 20 turnos irritando o Neo de verdade — e
    porque entre "não mexer" e "zerar tudo" (o /reset) não havia nada.
    """
    if _pks is None:
        raise HTTPException(status_code=503, detail="PKS service not ready")
    try:
        uid = UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="user_id must be a UUID") from exc

    state = await _pks.set_user_affect(
        user_id=uid,
        stats=payload.stats,
        grudge_count=payload.grudge_count,
        trigger=payload.trigger,
        reason=payload.reason,
    )
    if state is None:
        raise HTTPException(status_code=404, detail="No primary entity for this user")
    return {"ok": True, "stats": state.stats.as_dict(), "updated_at": state.updated_at}


@router.post("/reset")
async def reset_affect(user_id: str = Query(...)):
    """Zera o estado para os baselines; limpa histórico recente e rancor."""
    if _pks is None:
        raise HTTPException(status_code=503, detail="PKS service not ready")
    try:
        uid = UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="user_id must be a UUID") from exc

    # Zerar o afeto tem que desfazer o que a birra aprontou: senão o estado volta
    # ao normal e a luz continua apagada.
    reverted = await _revert_birra(uid, reason="reset")
    fresh = await _pks.reset_user_affect(user_id=uid)
    if fresh is None:
        raise HTTPException(status_code=404, detail="No primary entity for this user")
    return {"ok": True, "stats": fresh.stats.as_dict(), "birra_reverted": reverted}


# ── Modo Birra (docs/modo-birra-plan.md §10) ─────────────────────────────────
# Sob /affect de propósito: é um estado derivado do afeto e mora no mesmo card do
# painel. (O plano dizia /api/v1/birra; ficou aninhado para não duplicar wiring.)

async def _revert_birra(user_id: UUID, *, reason: str) -> int:
    """Desfaz os atos pendentes deste usuário. Nunca levanta."""
    try:
        from core.agent.birra.revert import revert_pending
        from core.agent.birra.store import BirraStore
        from core.skills.home_assistant.adapter import HomeAssistantAdapter

        adapter = HomeAssistantAdapter()
        if not adapter.is_configured:
            return 0
        return await revert_pending(
            BirraStore(_pks.db), adapter, user_id=user_id, reason=reason
        )
    except Exception:  # noqa: BLE001
        return 0


@router.get("/birra")
async def get_birra(user_id: str = Query(...)):
    """Episódio atual + atos, incluindo o que ainda falta desfazer."""
    if _pks is None:
        raise HTTPException(status_code=503, detail="PKS service not ready")
    try:
        uid = UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="user_id must be a UUID") from exc

    from core.agent.birra.store import BirraStore

    state = await _pks.get_user_affect(user_id=uid)
    episode = getattr(state, "birra", None)
    pending = await BirraStore(_pks.db).pending(user_id=uid)
    return {
        "enabled": get_settings().neo_birra_enabled,
        "episode": episode.model_dump() if episode else None,
        "active": bool(episode and episode.active),
        "pending": [
            {
                "id": str(a.id),
                "entity_id": a.entity_id,
                "mecanica": a.mecanica,
                "fala": a.fala,
                "requested": a.requested,
                "performed": a.performed,
                "revert_at": a.revert_at.isoformat() if a.revert_at else None,
            }
            for a in pending
        ],
    }


@router.post("/birra/end")
async def end_birra(user_id: str = Query(...)):
    """Botão de pânico: encerra o episódio e desfaz tudo que estiver pendente."""
    if _pks is None:
        raise HTTPException(status_code=503, detail="PKS service not ready")
    try:
        uid = UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="user_id must be a UUID") from exc

    reverted = await _revert_birra(uid, reason="panic")
    await _pks.update_birra_episode(user_id=uid, decision=None, release_reason="panic")
    return {"ok": True, "reverted": reverted}
