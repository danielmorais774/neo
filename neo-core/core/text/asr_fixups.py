"""Backstop determinístico para texto vindo de ASR (entrada por voz).

O corretor de verdade mora no neo-voice (`service/stt_text.py`): léxico completo,
camada fonética, descarte de eco do wake word. Este módulo é a rede embaixo dele —
existe porque o neo-core NÃO pode depender de o cliente ter passado pelo neo-voice
atualizado: áudio do WhatsApp, o app mobile ou um cliente novo entram por outra
porta e chegam aqui com o nome torto do mesmo jeito.

Por isso o escopo aqui é MÍNIMO e não duplica o léxico do neo-voice: só o nome do
assistente, só em posição de vocativo. O resto da tolerância é do modelo, via
`_VOICE_INPUT_INSTRUCTION` (docs/voice-input-normalization-plan.md §4).

Só roda quando ``metadata["input_source"] == "voice"``. Em texto digitado, nunca.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata

logger = logging.getLogger(__name__)

ASSISTANT_NAME = os.environ.get("NEO_ASSISTANT_NAME", "Neo")

# As variantes que o Parakeet realmente escreve (medidas no corpus de produção:
# neil 6x, new 6x, nil 3x, knew 2x, nir 1x — e "neo" ZERO vezes em 368 turnos).
_VARIANTS = (
    "neil", "niel", "nil", "nir", "niu", "nio", "neu", "neal", "nell",
    "knew", "new", "nieu", "niw",
)

# Palavra de chamamento que ancora o vocativo (o mesmo conjunto do stt_text.py).
_LEAD = r"(?:ei|oi|ol[aá]|al[oô]|opa|hey|hei|hi|fala|fale|escuta|escute|olha|olhe|testando)"

# Vocativo em três posições ancoradas, e só nelas:
#   1. início do enunciado seguido de vírgula   -> "Neil, que horas são?"
#   2. depois de vírgula/ponto                  -> "Foi tá bom, Nir."
#   3. depois de palavra de chamamento          -> "Alô New testando"
# Fora disso não mexe: "por que isso retorna nil?" é conversa de dev, não vocativo.
_PATTERNS = [
    re.compile(rf"(?i)(^|(?<=[.!?;:,])\s*)({'|'.join(_VARIANTS)})(?=\s*[,.!?;:]|\s+(?:que|qual|quem|como|quando|onde|voc[eê]|tu|me|te|bota|fala|liga|desliga|toca|manda|abre)\b)"),
    re.compile(rf"(?i)(\b{_LEAD}[,\s]+)({'|'.join(_VARIANTS)})\b"),
]


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()


def canonicalize_assistant_name(text: str) -> tuple[str, list[str]]:
    """Devolve (texto, variantes_trocadas). Nunca levanta."""
    if not text or not text.strip():
        return text, []
    try:
        found: list[str] = []
        out = text
        for rx in _PATTERNS:
            def _sub(m: re.Match) -> str:
                variant = m.group(2)
                if _fold(variant) == _fold(ASSISTANT_NAME):
                    return m.group(0)
                found.append(variant)
                return f"{m.group(1)}{ASSISTANT_NAME}"
            out = rx.sub(_sub, out)
        return out, found
    except Exception:  # pragma: no cover - defensivo: nunca derruba o turno
        logger.exception("[ASR] canonicalize_assistant_name falhou — texto cru mantido")
        return text, []


def is_voice_input(metadata: dict | None) -> bool:
    """``input_source == "voice"``. Nunca inferido de ``mode == "live"`` (saída)."""
    if not metadata:
        return False
    return str(metadata.get("input_source", "")).strip().lower() == "voice"
