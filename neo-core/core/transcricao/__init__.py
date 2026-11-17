"""Transcrição de vídeo e áudio pelo Groq (docs/transcricao-groq.md).

O sandbox tira o áudio com ffmpeg, o neo-core manda ao Whisper do Groq com a
chave que o neo-panel grava, e o resultado volta para o /workspace em .txt e .srt.
Com "só a cota gratuita" ligado, o Neo para antes de passar dos limites do plano
gratuito.
"""

from core.transcricao import config
from core.transcricao.tools import (
    TOOL_NAME,
    TRANSCRICAO_TOOL_NAMES,
    TRANSCRICAO_TOOLS,
    TranscricaoExecutor,
)

__all__ = [
    "TOOL_NAME",
    "TRANSCRICAO_TOOLS",
    "TRANSCRICAO_TOOL_NAMES",
    "TranscricaoExecutor",
    "config",
]
