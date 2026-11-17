"""neo-browser-agent — wrapper FastAPI do subagente browser-use.

Fino de propósito: o "background" (fila, retry, aviso proativo) é papel do
JobManager do neo-core; aqui é só sessão persistente + rodar o Agent e devolver
o resultado (docs/browser-agent-plan.md §3.1). Sem porta pública — só a rede
docker `browser-net` alcança este serviço.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import shots
from session_manager import SessionBusyError, SessionManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("neo-browser-agent")

#: Default do modelo — só vale quando o neo-core NÃO manda um no corpo da task.
#: O dono da escolha é o role `browser_agent` do painel (modules/model_config no
#: neo-core); aqui é o piso, para o serviço subir sozinho e para o /health ter o
#: que mostrar. Enquanto isto era a ÚNICA fonte, trocar o modelo exigia mexer no
#: compose e recriar o container.
LLM_MODEL = os.environ.get("NEO_BROWSER_LLM", "google/gemini-2.5-flash")
LLM_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
LLM_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
CHROMIUM = os.environ.get("NEO_BROWSER_CHROMIUM", "/usr/bin/chromium")
TTL_S = int(os.environ.get("NEO_BROWSER_SESSION_TTL", "900"))
MAX_WARM = int(os.environ.get("NEO_BROWSER_MAX_WARM", "1"))
MAX_QUEUE = int(os.environ.get("NEO_BROWSER_MAX_QUEUE", "2"))
TASK_TIMEOUT_S = int(os.environ.get("NEO_BROWSER_TASK_TIMEOUT", "570"))
MAX_STEPS_CAP = int(os.environ.get("NEO_BROWSER_MAX_STEPS_CAP", "100"))

_SID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

manager = SessionManager(
    profiles_root="/data/profiles",
    chromium_path=CHROMIUM,
    ttl_s=TTL_S,
    max_warm=MAX_WARM,
    max_queue=MAX_QUEUE,
    task_timeout_s=TASK_TIMEOUT_S,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    manager.start_reaper()
    logger.info(
        "up — llm=%s ttl=%ss max_warm=%s key=%s",
        LLM_MODEL, TTL_S, MAX_WARM, "ok" if LLM_API_KEY else "AUSENTE",
    )
    yield
    await manager.shutdown()


app = FastAPI(title="neo-browser-agent", lifespan=lifespan)


class TaskRequest(BaseModel):
    task: str = Field(min_length=3)
    #: Modelo desta task (ID do OpenRouter). Ausente/vazio = usa o LLM_MODEL.
    model: str | None = None
    context: str | None = None
    sensitive_data: dict | None = None  # F3: {dominio: {placeholder: valor}}
    max_steps: int | None = None
    # Prints da tela (shots.py): "auto" = só quando dá erro (default), "always" =
    # também no fim de uma task bem-sucedida, "never" = nenhum.
    screenshots: str | None = None
    max_screenshots: int | None = None


def _make_llm(model: str | None = None):
    # Import tardio: browser-use resolve exports em lazy __getattr__; manter o
    # boot do wrapper rápido e o erro de config legível.
    from browser_use import ChatOpenAI

    return ChatOpenAI(
        model=(model or "").strip() or LLM_MODEL,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
    )


def _check_sid(sid: str) -> str:
    if not _SID_RE.match(sid):
        raise HTTPException(status_code=400, detail="sid inválido (use [a-zA-Z0-9_-]{1,64})")
    return sid


@app.get("/health")
async def health():
    return {
        "ok": True,
        # É o DEFAULT, não necessariamente o que a última task usou: o neo-core
        # manda o modelo por task. Dizer "llm_model" e nada mais foi o que fez
        # este valor parecer a verdade única do subsistema.
        "llm_model_default": LLM_MODEL,
        "llm_model": LLM_MODEL,
        "llm_configured": bool(LLM_API_KEY),
        "warm_sessions": manager.warm_sessions(),
    }


@app.post("/sessions/{sid}/tasks")
async def run_task(sid: str, req: TaskRequest):
    _check_sid(sid)
    if not LLM_API_KEY:
        raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY não configurada")
    max_steps = max(3, min(req.max_steps or 40, MAX_STEPS_CAP))
    try:
        result = await manager.run_task(
            sid,
            task=req.task,
            llm=_make_llm(req.model),
            context=req.context,
            sensitive_data=req.sensitive_data,
            max_steps=max_steps,
            screenshots=shots.normalize_mode(req.screenshots),
            max_screenshots=shots.normalize_max(req.max_screenshots),
        )
    except SessionBusyError as e:
        raise HTTPException(status_code=429, detail=str(e))
    return result


@app.post("/sessions/{sid}/screenshot")
async def take_screenshot(sid: str):
    """Print da tela agora (sem rodar task) — o "me mostra o que tá aí" do chat."""
    _check_sid(sid)
    return await manager.screenshot(sid)


@app.get("/sessions/{sid}")
async def session_state(sid: str):
    _check_sid(sid)
    return await manager.state(sid)


@app.delete("/sessions/{sid}")
async def close_session(sid: str, purge: bool = False):
    _check_sid(sid)
    return await manager.close(sid, purge=purge)
