"""Persona do Neo — FONTE ÚNICA.

Todo o tom/voz/gatilho do Neo vive em `persona.md`, ao lado deste módulo.
Nenhum pipeline deve carregar texto de persona por conta própria: chame
`neo_persona("full")` (núcleo cognitivo, conversa de verdade) ou
`neo_persona("slim")` (pipelines de comando voltados ao usuário, ex.: Home
Assistant — só a voz, sem o gatilho do nome, que o roteador já desvia pro
cognitivo).

`persona.md` é marcado com blocos:
    <!-- neo:core -->  ... voz base (entra em full E slim)  ... <!-- /neo:core -->
    <!-- neo:full -->  ... gatilho + exemplos (só no full)  ... <!-- /neo:full -->
Texto fora dos blocos (o cabeçalho de identidade) entra em ambos.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

_PERSONA_PATH = Path(__file__).parent / "persona.md"

# Fallback mínimo caso o arquivo suma — nunca deixa o Neo sem voz (nem "aboiolado").
_FALLBACK = (
    "Você é o Neo. Frio, curto, competente. Frases curtas e declarativas; "
    "entrega o que foi pedido e para. Sem bajulação, sem emoji, sem hedge, "
    "sem disclaimers. Sempre em português do Brasil."
)

_FULL_BLOCK = re.compile(r"<!--\s*neo:full\s*-->.*?<!--\s*/neo:full\s*-->", re.DOTALL)
_MARKERS = re.compile(r"<!--\s*/?neo:(?:core|full)\s*-->")
_BLANK_RUN = re.compile(r"\n{3,}")


@lru_cache(maxsize=2)
def neo_persona(mode: Literal["full", "slim"] = "full") -> str:
    """Retorna a persona do Neo montada a partir da fonte única.

    - ``full``: identidade + voz base + gatilho do nome + exemplos.
    - ``slim``: identidade + voz base (sem gatilho/exemplos).

    Resultado é cacheado; reinicie o processo após editar ``persona.md``.
    """
    try:
        text = _PERSONA_PATH.read_text(encoding="utf-8")
    except OSError:
        logger.warning("persona.md não encontrado em %s — usando fallback", _PERSONA_PATH)
        return _FALLBACK

    if mode == "slim":
        text = _FULL_BLOCK.sub("", text)
    text = _MARKERS.sub("", text)
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip()
