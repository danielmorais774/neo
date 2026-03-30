"""Token counter — contagem precisa de tokens usando tiktoken."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING

import tiktoken

if TYPE_CHECKING:
    from shared.types import Message

logger = logging.getLogger(__name__)

# Overhead por mensagem no formato ChatML do OpenAI
_MSG_OVERHEAD = 4  # <|im_start|>{role}\n ... <|im_end|>\n
_REPLY_OVERHEAD = 3  # every reply is primed with <|im_start|>assistant<|im_sep|>


@lru_cache(maxsize=8)
def _get_encoding(model: str) -> tiktoken.Encoding:
    """Carrega encoding do tiktoken com cache."""
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        logger.debug(f"Modelo '{model}' não encontrado no tiktoken, usando cl100k_base")
        return tiktoken.get_encoding("cl100k_base")


class TokenCounter:
    """Contagem de tokens precisa e eficiente.

    Usa tiktoken para contar tokens da mesma forma que a API do OpenAI.
    Cache interno do encoding para evitar recarregar a cada chamada.
    """

    def __init__(self, model: str = "gpt-4") -> None:
        self.model = model
        self._encoding = _get_encoding(model)

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def count(self, text: str) -> int:
        """Conta tokens em um texto simples."""
        if not text:
            return 0
        return len(self._encoding.encode(text))

    def count_messages(self, messages: list[Message]) -> int:
        """Conta tokens de uma lista de mensagens no formato chat.

        Inclui overhead de formatação (role tags, separadores).
        """
        total = 0
        for msg in messages:
            total += _MSG_OVERHEAD
            total += self.count(msg.role.value)
            total += self.count(msg.content)
            if msg.name:
                total += self.count(msg.name) + 1  # name + separador
        total += _REPLY_OVERHEAD
        return total

    def estimate_remaining(self, messages: list[Message], max_tokens: int) -> int:
        """Calcula tokens disponíveis dado o histórico atual."""
        used = self.count_messages(messages)
        return max(0, max_tokens - used)
