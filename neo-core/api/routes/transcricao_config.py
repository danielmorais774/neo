"""neo-panel → Configurações → Transcrição: chave do Groq, modelo e "só a cota gratuita".

- ``GET  /transcricao-config``: estado (de onde vem a chave e os últimos 4
  caracteres, o modelo, o interruptor e o que resta da cota);
- ``PUT  /transcricao-config``: ``{so_gratuita?, modelo?}``;
- ``PUT  /transcricao-config/key``: ``{api_key}``. A chave é testada na lista de
  modelos do Groq (não gasta cota) ANTES de gravar;
- ``DELETE /transcricao-config/key``: volta a valer a ``GROQ_API_KEY`` do ``.env``;
- ``GET  /transcricao-config/modelos``: os Whisper da conta, com a chave em uso.

Tudo vale na hora, sem reiniciar (``core/transcricao/config.py``). Protegido
pelo JWT do painel (ligado em ``api/main.py``).
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.transcricao import config, groq
from core.transcricao.cota import cota_do_processo

router = APIRouter(prefix="/transcricao-config", tags=["transcricao-config"])

_db = None
_tool_montada = False
_MODELO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")


def set_dependencies(db, *, tool_montada: bool) -> None:
    """Chamado no lifespan. Sem sandbox, a tool não é montada e a aba avisa."""
    global _db, _tool_montada
    _db = db
    _tool_montada = tool_montada


class OpcoesUpdate(BaseModel):
    so_gratuita: bool | None = None
    modelo: str | None = None


class ChaveUpdate(BaseModel):
    api_key: str


def _pronto() -> None:
    if _db is None:
        raise HTTPException(status_code=503, detail="Service not ready")


async def _payload() -> dict:
    from shared.ajustes import ajustes

    limites = ajustes().transcricao.cota
    try:
        cota = await cota_do_processo(_db).estado()
    except Exception:  # noqa: BLE001 — a cota é informativa
        cota = {}
    return {
        "available": _tool_montada,
        "key": {
            "source": config.origem_da_chave(),
            "last4": config.final_da_chave(),
            "stored": config.origem_da_chave() == "painel" or config.chave_quebrada(),
            "broken": config.chave_quebrada(),
            "can_store": config.cofre_disponivel(),
        },
        "so_gratuita": config.so_gratuita(),
        "modelo": config.modelo(),
        "modelo_padrao": config.MODELO_PADRAO,
        "cota": cota,
        "limites": {
            "audio_s_por_hora": limites.audio_s_por_hora,
            "audio_s_por_dia": limites.audio_s_por_dia,
            "pedidos_por_minuto": limites.pedidos_por_minuto,
            "pedidos_por_dia": limites.pedidos_por_dia,
            "arquivo_max_mb": limites.arquivo_max_mb,
        },
    }


@router.get("")
async def get_config() -> dict:
    _pronto()
    return await _payload()


@router.put("")
async def update_config(payload: OpcoesUpdate) -> dict:
    _pronto()
    modelo = payload.modelo.strip() if payload.modelo is not None else None
    if modelo is not None and not _MODELO_RE.match(modelo):
        raise HTTPException(status_code=400, detail="nome de modelo inválido.")
    try:
        await config.gravar_opcoes(_db, so_gratuita=payload.so_gratuita, modelo=modelo)
    except config.ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return await _payload()


@router.put("/key")
async def update_key(payload: ChaveUpdate) -> dict:
    _pronto()
    chave = (payload.api_key or "").strip()
    if not chave:
        raise HTTPException(status_code=400, detail="a chave está vazia.")
    if not config.cofre_disponivel():
        raise HTTPException(
            status_code=409,
            detail="o cofre está desligado (NEO_CRED_KEY ausente no .env): não há como guardar a chave cifrada.",
        )
    try:
        await groq.listar_modelos(chave)
    except groq.GroqErro as e:
        raise HTTPException(status_code=400, detail=f"a chave não passou no teste: {e.mensagem}") from e
    try:
        await config.gravar_chave(_db, chave)
    except config.ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return await _payload()


@router.delete("/key")
async def delete_key() -> dict:
    _pronto()
    await config.apagar_chave(_db)
    return await _payload()


@router.get("/modelos")
async def list_models() -> dict:
    _pronto()
    chave = config.chave()
    if not chave:
        raise HTTPException(status_code=409, detail="não há chave do Groq em uso.")
    try:
        modelos = await groq.listar_modelos(chave)
    except groq.GroqErro as e:
        raise HTTPException(status_code=502, detail=e.mensagem) from e
    return {"modelos": modelos, "atual": config.modelo()}
