"""Context compactor — sumarização inteligente de conversas longas.

Quando o contexto excede o budget de tokens, divide a conversa em chunks,
sumariza cada um via LLM em paralelo, e mescla em um resumo coerente.
Preserva identificadores críticos (UUIDs, URLs, paths, IPs).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from shared.types import CompactionResult, Message, MessageRole

if TYPE_CHECKING:
    from core.agent.token_counter import TokenCounter
    from core.llm import LLMRouter

logger = logging.getLogger(__name__)

# Regex para extrair identificadores que devem sobreviver à compactação
_IDENTIFIER_PATTERNS = [
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I),  # UUID
    re.compile(r"https?://[^\s\"'<>]+"),                                                      # URL
    re.compile(r"(?:/[\w.-]+){2,}"),                                                           # Unix paths
    re.compile(r"[A-Za-z]:\\(?:[\w.-]+\\)*[\w.-]+"),                                           # Windows paths
    re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),                                    # IPv4
]

_COMPACTION_PROMPT = """Resuma a conversa abaixo de forma concisa mas preservando:
- Nomes de ferramentas usadas e seus resultados
- UUIDs, URLs, caminhos de arquivo e IPs mencionados
- Decisões tomadas e erros encontrados
- Contexto necessário para continuar a conversa

Responda APENAS com o resumo, sem introdução ou conclusão.

--- CONVERSA ---
{conversation}
"""


def _extract_identifiers(text: str) -> list[str]:
    """Extrai identificadores únicos (UUIDs, URLs, paths) de um texto."""
    found: set[str] = set()
    for pattern in _IDENTIFIER_PATTERNS:
        found.update(pattern.findall(text))
    return sorted(found)


def _chunk_messages(messages: list[Message], chunk_size: int) -> list[list[Message]]:
    """Divide mensagens em chunks de tamanho fixo."""
    chunks: list[list[Message]] = []
    for i in range(0, len(messages), chunk_size):
        chunks.append(messages[i : i + chunk_size])
    return chunks


class ContextCompactor:
    """Compacta contexto longo via sumarização LLM paralela.

    Inspirado no sistema de compaction do OpenClaw: divide em chunks,
    sumariza em paralelo, preserva identificadores, mescla resultado.
    """

    def __init__(
        self,
        llm_router: LLMRouter,
        token_counter: TokenCounter,
        chunk_size: int = 10,
    ) -> None:
        self._llm = llm_router
        self._tokens = token_counter
        self._chunk_size = chunk_size

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def should_compact(
        self,
        messages: list[Message],
        max_tokens: int,
        threshold: float = 0.75,
    ) -> bool:
        """Verifica se as mensagens excedem o threshold de tokens."""
        current = self._tokens.count_messages(messages)
        limit = int(max_tokens * threshold)
        return current > limit

    async def compact(
        self,
        messages: list[Message],
        target_tokens: int,
    ) -> CompactionResult:
        """Compacta mensagens preservando informações críticas.

        Args:
            messages: Mensagens a compactar (sem system messages).
            target_tokens: Budget de tokens para o resumo resultante.

        Returns:
            CompactionResult com resumo e metadados.
        """
        original_count = self._tokens.count_messages(messages)

        # Extrair identificadores de TODA a conversa antes de sumarizar
        full_text = "\n".join(m.content for m in messages)
        identifiers = _extract_identifiers(full_text)

        # Dividir em chunks e sumarizar em paralelo
        chunks = _chunk_messages(messages, self._chunk_size)
        logger.info(f"Compactando {len(messages)} msgs em {len(chunks)} chunks")

        summaries = await asyncio.gather(
            *[self._summarize_chunk(chunk) for chunk in chunks]
        )

        # Mesclar resumos
        merged = "\n\n".join(s for s in summaries if s)

        # Se os identificadores não estão no resumo, adicioná-los
        missing_ids = [i for i in identifiers if i not in merged]
        if missing_ids:
            merged += "\n\n[Identificadores preservados: " + ", ".join(missing_ids[:20]) + "]"

        # Verificar se cabe no target
        compacted_count = self._tokens.count(merged)
        if compacted_count > target_tokens:
            merged = self._truncate_to_fit(merged, target_tokens)
            compacted_count = self._tokens.count(merged)

        logger.info(f"Compactação: {original_count} → {compacted_count} tokens")

        return CompactionResult(
            summary=merged,
            preserved_identifiers=identifiers,
            original_token_count=original_count,
            compacted_token_count=compacted_count,
        )

    # ------------------------------------------------------------------
    # Métodos internos
    # ------------------------------------------------------------------

    async def _summarize_chunk(self, chunk: list[Message]) -> str:
        """Sumariza um chunk de mensagens via LLM."""
        conversation = "\n".join(
            f"[{m.role.value}] {m.content}" for m in chunk
        )

        prompt_text = _COMPACTION_PROMPT.format(conversation=conversation)
        messages = [
            Message(role=MessageRole.USER, content=prompt_text),
        ]

        try:
            provider = self._llm.get_provider()
            response = await provider.complete(
                messages=messages,
                model=self._llm.get_default_model(),
                temperature=0.3,  # baixa criatividade para resumos fiéis
                max_tokens=1024,
            )
            return response.get_content() or ""
        except Exception as e:
            logger.error(f"Falha ao sumarizar chunk: {e}")
            # Fallback: concatenar primeiras linhas de cada mensagem
            return "\n".join(m.content[:100] for m in chunk)

    def _truncate_to_fit(self, text: str, max_tokens: int) -> str:
        """Trunca texto mantendo início e fim."""
        words = text.split()
        if not words:
            return text

        # Tenta com porcentagens decrescentes até caber
        for keep_ratio in [0.8, 0.6, 0.4]:
            keep_count = int(len(words) * keep_ratio)
            half = keep_count // 2
            truncated = " ".join(words[:half]) + "\n[...]\n" + " ".join(words[-half:])
            if self._tokens.count(truncated) <= max_tokens:
                return truncated

        # Último recurso: só o início
        return " ".join(words[: len(words) // 4])
