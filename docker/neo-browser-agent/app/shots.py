"""Prints da tela do subagente — a evidência que o texto do resultado não dá.

Duas fontes, com papéis diferentes (docs/browser-agent-plan.md §5, F2):

- **Ao vivo** (:func:`capture_live`): ``Page.captureScreenshot`` via CDP no
  instante em que o chamador pede. É o print do ERRO — e por isso é tirado
  SEMPRE antes de qualquer ``_kill_browser``: depois de matar o Chromium não
  existe mais tela para fotografar.
- **Histórico** (:func:`from_history`): os prints por passo que a própria lib já
  guardou (``AgentHistoryList.screenshots()``). Mostram COMO se chegou ao erro e
  saem de graça — já estão em disco.

Formato: o print ao vivo sai em JPEG (uma página cheia em PNG passa de 1 MB; o
mesmo quadro em JPEG q75 fica na casa dos 150 KB) e os do histórico vêm em PNG
da lib, como estão. Tudo trafega em base64 no JSON da resposta — o neo-core
ingere no File Store e é ele quem vira `[[IMAGE:file:…]]` no chat.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os

logger = logging.getLogger(__name__)

MODES = ("auto", "always", "never")
DEFAULT_MODE = "auto"

# Teto duro: mais que isso não é evidência, é despejo de imagem no chat.
MAX_SHOTS_CAP = int(os.environ.get("NEO_BROWSER_SHOTS_CAP", "6"))
DEFAULT_MAX_SHOTS = int(os.environ.get("NEO_BROWSER_SHOTS_DEFAULT", "3"))
MAX_SHOT_BYTES = int(os.environ.get("NEO_BROWSER_SHOT_MAX_BYTES", str(4 * 1024 * 1024)))
MAX_TOTAL_BYTES = int(os.environ.get("NEO_BROWSER_SHOTS_MAX_TOTAL_BYTES", str(10 * 1024 * 1024)))
# Uma captura CDP em página pesada demora; travar a resposta do wrapper por causa
# de um print seria trocar o resultado (que existe) por uma foto (que é extra).
CAPTURE_TIMEOUT_S = int(os.environ.get("NEO_BROWSER_SHOT_TIMEOUT", "20"))
JPEG_QUALITY = int(os.environ.get("NEO_BROWSER_SHOT_QUALITY", "75"))

# Prioridade quando o orçamento aperta: o print do erro é o que o usuário pediu.
_PRIORITY = {"error": 0, "final": 1, "live": 1, "step": 2}


def normalize_mode(value: object) -> str:
    """``auto`` (só quando dá erro) · ``always`` (sempre) · ``never``."""
    mode = str(value or "").strip().lower()
    return mode if mode in MODES else DEFAULT_MODE


def normalize_max(value: object, default: int = DEFAULT_MAX_SHOTS) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, MAX_SHOTS_CAP))


def _shot(*, b64: str, mime: str, kind: str, caption: str, order: int, url: str | None) -> dict:
    return {
        "kind": kind,
        "caption": caption,
        "mime": mime,
        "b64": b64,
        "bytes": (len(b64) * 3) // 4,  # tamanho decodificado aproximado
        "url": url,
        "order": order,
    }


async def capture_live(
    browser,
    *,
    kind: str = "live",
    caption: str = "estado da tela",
    url: str | None = None,
    order: int = 10_000,
) -> dict | None:
    """Fotografa a tela AGORA. Devolve ``None`` (nunca levanta) se não der.

    Falhar aqui é normal e não pode contaminar o resultado da task: o Chromium
    pode ter morrido justamente no erro que se quer fotografar.
    """
    if browser is None:
        return None
    try:
        data = await asyncio.wait_for(
            browser.take_screenshot(format="jpeg", quality=JPEG_QUALITY),
            timeout=CAPTURE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("print ao vivo: timeout de %ss", CAPTURE_TIMEOUT_S)
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("print ao vivo falhou: %s", e)
        return None
    if not data:
        return None
    if isinstance(data, str):  # versões que já devolvem base64
        b64 = data
    else:
        b64 = base64.b64encode(data).decode("ascii")
    return _shot(b64=b64, mime="image/jpeg", kind=kind, caption=caption, order=order, url=url)


def from_history(history, *, limit: int, url: str | None = None) -> list[dict]:
    """Os prints por passo que a lib guardou — o caminho até o erro.

    Defensivo de propósito: a superfície do ``AgentHistoryList`` muda entre
    versões da browser-use (o pacote nem expõe ``__version__``).
    """
    if limit <= 0 or history is None:
        return []
    fn = getattr(history, "screenshots", None)
    if not callable(fn):
        return []
    try:
        raw = fn(n_last=limit, return_none_if_not_screenshot=False) or []
    except Exception as e:  # noqa: BLE001
        logger.warning("prints do histórico indisponíveis: %s", e)
        return []

    total = 0
    try:
        steps = getattr(history, "number_of_steps", None)
        total = int(steps()) if callable(steps) else 0
    except Exception:  # noqa: BLE001
        total = 0

    shots: list[dict] = []
    tail = [b for b in raw if b]
    first_step = max(1, total - len(tail) + 1) if total else 1
    for idx, b64 in enumerate(tail):
        step_no = first_step + idx
        caption = f"passo {step_no}" + (f" de {total}" if total else "")
        shots.append(
            _shot(b64=b64, mime="image/png", kind="step", caption=caption, order=idx, url=url)
        )
    return shots


def budget(shots: list[dict], *, max_count: int) -> list[dict]:
    """Corta pelo orçamento (nº, tamanho por print e total), preservando o erro.

    Escolhe por prioridade — print do erro antes dos passos, passo recente antes
    do antigo — e devolve em ordem CRONOLÓGICA, que é como se lê no chat: como
    chegou lá, e onde parou.
    """
    usable = [s for s in shots if s and s.get("b64") and s.get("bytes", 0) <= MAX_SHOT_BYTES]
    ranked = sorted(
        usable,
        key=lambda s: (_PRIORITY.get(s.get("kind", "step"), 3), -int(s.get("order") or 0)),
    )
    kept: list[dict] = []
    total = 0
    for shot in ranked:
        if len(kept) >= max_count:
            break
        size = int(shot.get("bytes") or 0)
        if total + size > MAX_TOTAL_BYTES:
            continue
        total += size
        kept.append(shot)
    return sorted(kept, key=lambda s: int(s.get("order") or 0))


def history_of(agent) -> object | None:
    """Histórico parcial de um agente que estourou/crashou (não retornou nada).

    No caminho feliz o histórico é o retorno de ``agent.run()``; quando o run
    morre no meio, o que existe é o acumulado em ``agent.state.history`` — é
    dele que saem os prints dos passos que levaram ao erro.
    """
    hist = getattr(getattr(agent, "state", None), "history", None)
    return hist if hist is not None and callable(getattr(hist, "screenshots", None)) else None
