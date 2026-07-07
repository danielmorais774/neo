"""Affect API routes — expõe o estado emocional do Neo por usuário para o painel.

GET devolve o estado bruto + pós-decay ao vivo + derivados (bandas, dominante,
humor, postura), para o painel não recalcular regra de negócio. POST /reset é a
válvula de escape admin: volta tudo para os baselines de personalidade.
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

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


@router.post("/reset")
async def reset_affect(user_id: str = Query(...)):
    """Zera o estado para os baselines; limpa histórico recente e rancor."""
    if _pks is None:
        raise HTTPException(status_code=503, detail="PKS service not ready")
    try:
        uid = UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="user_id must be a UUID") from exc

    fresh = await _pks.reset_user_affect(user_id=uid)
    if fresh is None:
        raise HTTPException(status_code=404, detail="No primary entity for this user")
    return {"ok": True, "stats": fresh.stats.as_dict()}
