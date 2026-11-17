"""Como o Neo edita arquivos do disco do usuário — escolhido no neo-panel (aba Arquivos).

``engine``:

* ``neo`` — o próprio núcleo cognitivo lê (``read_user_file``) e troca trechos
  exatos (``edit_user_file``);
* ``opencode`` — o Neo orquestra e o opencode instalado no desktop faz a edição
  (``delegate_file_edit``), com o modelo escolhido aqui. O desktop roda o opencode
  numa cópia do arquivo e aplica o resultado pelas mesmas regras (hash, backup,
  card com Desfazer) — ver ``neo-desktop-tauri/src-tauri/src/services/file_ops.rs``.

Persistido em ``user_preference`` (service="neo-desktop", category="file-edit",
key="config"), no mesmo padrão do voice-config: um registro por usuário + o global.
O modelo do opencode é decisão do painel, nunca do LLM — o core injeta no
``tool_request`` e ignora o que o cérebro tiver escrito.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

PREF_SERVICE = "neo-desktop"
PREF_CATEGORY = "file-edit"
PREF_KEY = "config"

ENGINES: tuple[str, ...] = ("neo", "opencode")

DEFAULTS: dict[str, Any] = {
    "engine": "neo",
    # A OpenRouter do próprio usuário (credencial no cofre do opencode).
    "opencode_model": "openrouter/deepseek/deepseek-v4.1-flash",
    "opencode_timeout_s": 300,
}

#: A config é lida a cada turno cognitivo (e a cada retomada): sem cache seria
#: uma query por rodada de tool.
_CACHE_TTL_S = 15.0
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def normalize_file_edit_config(data: Any) -> dict[str, Any]:
    """Valores válidos sobre os defaults — lixo no banco não derruba o turno."""
    cfg = dict(DEFAULTS)
    if not isinstance(data, dict):
        return cfg
    engine = str(data.get("engine") or "").strip().lower()
    if engine in ENGINES:
        cfg["engine"] = engine
    model = str(data.get("opencode_model") or "").strip()
    if model and "/" in model and len(model) <= 200 and not any(c.isspace() for c in model):
        cfg["opencode_model"] = model
    try:
        cfg["opencode_timeout_s"] = max(30, min(1800, int(data.get("opencode_timeout_s"))))
    except (TypeError, ValueError):
        pass
    return cfg


async def get_file_edit_config(db: Any, user_id: UUID | None = None) -> dict[str, Any]:
    """Config do usuário; sem registro dele, a global; sem nenhuma, os defaults."""
    key = str(user_id or "global")
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL_S:
        return dict(hit[1])

    cfg = dict(DEFAULTS)
    if db is not None:
        for uid in ((user_id, None) if user_id else (None,)):
            try:
                prefs = await db.get_preferences(
                    user_id=uid, service=PREF_SERVICE, category=PREF_CATEGORY
                )
            except Exception as e:  # noqa: BLE001 — config nunca derruba o turno
                logger.warning("[file-edit-config] leitura falhou: %s", e)
                break
            found = next((p for p in prefs if getattr(p, "key", None) == PREF_KEY), None)
            if found is None:
                continue
            try:
                cfg = normalize_file_edit_config(json.loads(found.value))
            except (ValueError, TypeError):
                logger.warning("[file-edit-config] valor inválido no banco: %r", found.value)
            break
    _cache[key] = (now, cfg)
    return dict(cfg)


def invalidate_file_edit_config_cache() -> None:
    _cache.clear()
