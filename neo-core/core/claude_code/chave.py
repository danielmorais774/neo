"""A chave do Claude Code integrado — o neo-panel liga e desliga, sem reiniciar.

Antes era só ``CLAUDE_CODE_ENABLED`` no ``.env``, e mudar exigia recriar o
container. Agora o ``.env`` é só o PADRÃO de quando o painel nunca mexeu; o que o
painel grava (``user_preference`` global: service="neo", category="claude-code",
key="enabled") manda, e vale na hora: o core é um processo só (uvicorn com o gRPC
dentro), e a gravação atualiza este módulo.

A leitura é síncrona porque quem pergunta não é todo async (o viés do roteador, o
filtro de tools): ``ligado()`` devolve o último valor conhecido, e ``carregar``
lê do banco no boot. Desligada, nada novo vai ao Claude — nem tarefa, nem
resposta, nem ajuste; o que já está rodando termina sozinho.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

PREF_SERVICE = "neo"
PREF_CATEGORY = "claude-code"
PREF_KEY = "enabled"

#: As tools que somem do Neo com a chave desligada. As duas últimas são as
#: sessões que o DANIEL usa (docs/ambiente-de-trabalho-plan.md); a chave própria
#: delas no painel é a F6 do plano.
TOOLS = ("claude_task", "claude_reply", "claude_sessoes", "claude_enviar")

#: None = o painel nunca gravou; vale o `.env`.
_valor: bool | None = None


def padrao() -> bool:
    """O ``CLAUDE_CODE_ENABLED`` do ``.env``."""
    from shared.config import get_settings

    return bool(getattr(get_settings(), "claude_code_enabled", False))


def ligado() -> bool:
    return padrao() if _valor is None else _valor


def origem() -> str:
    return "env" if _valor is None else "painel"


async def carregar(db: Any) -> bool:
    """Lê o que o painel gravou. Banco fora: fica o que estava (no boot, o `.env`)."""
    global _valor
    try:
        prefs = await db.get_preferences(user_id=None, service=PREF_SERVICE, category=PREF_CATEGORY)
    except Exception as e:  # noqa: BLE001 — a chave nunca derruba o boot
        logger.warning("[claude] não li a chave do painel: %s", e)
        return ligado()
    achada = next((p for p in prefs if getattr(p, "key", None) == PREF_KEY), None)
    if achada is not None:
        _valor = str(achada.value).strip().lower() in ("true", "1", "on", "sim")
    return ligado()


async def gravar(db: Any, valor: bool) -> bool:
    global _valor
    await db.upsert_preference(
        category=PREF_CATEGORY,
        key=PREF_KEY,
        value="true" if valor else "false",
        user_id=None,
        service=PREF_SERVICE,
        source_message="neo-panel settings",
    )
    _valor = bool(valor)
    logger.info("[claude] Claude Code integrado %s pelo painel", "LIGADO" if valor else "DESLIGADO")
    return _valor


def _esquecer() -> None:
    """Só para teste: volta a valer o `.env`."""
    global _valor
    _valor = None
