"""Prints do browser → File Store → `[[IMAGE:file:…]]` na bolha do chat.

O container ``neo-browser-agent`` devolve os prints em base64 dentro do JSON da
task (docker/neo-browser-agent/app/shots.py). Aqui eles viram arquivos do
catálogo (``origin="job"``, retenção temp) e, daí em diante, o único que viaja é
o ``file_id`` — os bytes NUNCA entram no prompt, exatamente como na feature de
imagens inline (o marcador é resolvido no cliente, que baixa do File Store).

Por que não passar o caminho do arquivo: o browser roda em OUTRO container, com
volume próprio; o File Store é a única superfície que os dois lados enxergam.
"""

from __future__ import annotations

import base64
import binascii
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Teto por print no lado do core (o wrapper já corta antes; isto é a rede de
# segurança contra um payload absurdo vindo do container).
MAX_SHOT_BYTES = 8 * 1024 * 1024
MAX_SHOTS = 6

_EXT_BY_MIME = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def _decode(b64: Any) -> bytes | None:
    if not isinstance(b64, str) or not b64.strip():
        return None
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not data or len(data) > MAX_SHOT_BYTES:
        return None
    return data


async def ingest_shots(
    store: Any,
    shots: list[dict] | None,
    *,
    user_id: Any = None,
    session_id: Any = None,
    label: str = "browser",
    source: dict[str, Any] | None = None,
) -> list[dict]:
    """Ingere os prints e devolve ``[{file_id, caption, kind, url}]``.

    Nunca levanta: print é evidência EXTRA. Se o File Store estiver desligado ou
    a quota estourar, a tarefa continua valendo com o texto que já tem — o que
    não pode acontecer é o job falhar por causa de uma foto.
    """
    if store is None or not shots:
        return []
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    out: list[dict] = []
    for idx, shot in enumerate(shots[:MAX_SHOTS], start=1):
        if not isinstance(shot, dict):
            continue
        data = _decode(shot.get("b64"))
        if data is None:
            logger.warning("[Browser] print %s descartado (base64 inválido ou grande demais)", idx)
            continue
        mime = str(shot.get("mime") or "image/png")
        ext = _EXT_BY_MIME.get(mime, "png")
        try:
            stored = await store.ingest_bytes(
                data,
                name=f"{label}-{stamp}-{idx}.{ext}",
                origin="job",
                mime=mime,
                user_id=user_id,
                session_id=session_id,
                retention="temp",
                source={"kind": "browser_screenshot", **(source or {}), "shot": shot.get("kind")},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[Browser] print %s não entrou no File Store: %s", idx, e)
            continue
        out.append({
            "file_id": str(stored.id),
            "caption": str(shot.get("caption") or "print do browser")[:120],
            "kind": str(shot.get("kind") or "step"),
            "url": (str(shot.get("url")) if shot.get("url") else None),
        })
    return out


def image_markers(images: list[dict] | None) -> list[str]:
    """Marcadores `[[IMAGE:file:<id>|legenda]]` prontos para a resposta."""
    markers: list[str] = []
    for img in (images or [])[:MAX_SHOTS]:
        if not isinstance(img, dict):
            continue
        file_id = str(img.get("file_id") or "").strip()
        if not file_id:
            continue
        # `]` fecharia o marcador no meio e `|` abriria outra legenda: a legenda
        # vem de fora (URL de página, texto de erro), então é entrada a sanear.
        caption = " ".join(str(img.get("caption") or "").replace("]", "").replace("|", " ").split())
        markers.append(f"[[IMAGE:file:{file_id}|{caption}]]" if caption else f"[[IMAGE:file:{file_id}]]")
    return markers
