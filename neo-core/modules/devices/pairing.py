"""Pareamento de device — códigos de 6 dígitos e tokens por device.

O problema que isto resolve (docs/device-control-plan.md §0): o desktop nunca
teve credencial. O ``user_id`` sai de ``NEO_USER_ID`` com fallback hardcoded
(``neo-desktop-tauri/src-tauri/src/lib.rs``), e o ``ws_router`` é montado sem
JWT — ou seja, hoje qualquer cliente que alcance a :8000 se registra como
qualquer device de qualquer usuário. Enquanto o cardápio era "aparecer numa
lista", isso era feio; com ``suspend``/``shutdown`` vira "desligar a máquina de
alguém".

O fluxo, mínimo que entrega dono verificável sem construir um login inteiro:

1. o painel (autenticado) chama ``POST /devices/pairing-code`` → código de 6
   dígitos, TTL de 5 min, amarrado ao ``user_id``;
2. o desktop chama ``POST /devices/pair`` com o código + sua identidade →
   recebe um ``device_token`` (mostrado UMA vez) e o guarda em disco;
3. todo canal do device passa a carregar o token, e o core resolve
   ``token → (user_id, device_id)`` **ignorando** o ``user_id`` que o cliente
   mandar.

Os códigos vivem em memória de propósito: são efêmeros (5 min), o core é um
processo só, e um restart durante o pareamento é indistinguível de um código
expirado — o usuário gera outro. Persistir isso seria uma tabela para nada.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from uuid import UUID

logger = logging.getLogger(__name__)

# TTL curto: o código é digitado em segundos, não guardado.
PAIRING_CODE_TTL_S = 300
# Um código vivo por usuário — gerar de novo invalida o anterior (senão sobram
# códigos válidos esquecidos por aí, cada um deles uma chave da casa).


@dataclass
class _PendingCode:
    user_id: UUID
    expires_at: float
    #: `device_id` da MÁQUINA a que o aparelho pareado vai ficar vinculado.
    #: Só existe em código emitido pelo próprio desktop
    #: (docs/browser-surface-plan.md §4): é ele que prova de qual máquina se
    #: trata, porque a extensão não tem como provar e não pode declarar.
    host_device_id: str | None = None


_codes: dict[str, _PendingCode] = {}


def _purge_expired(now: float | None = None) -> None:
    now = now or time.time()
    for code in [c for c, p in _codes.items() if p.expires_at <= now]:
        _codes.pop(code, None)


def generate_pairing_code(
    user_id: UUID, *, host_device_id: str | None = None
) -> tuple[str, int]:
    """Gera um código de pareamento para um usuário. Devolve (código, ttl_s).

    ``host_device_id`` amarra o que for pareado com este código à MÁQUINA que o
    pediu. Usado pela rota que o desktop chama com o token dele: a autoridade
    dele é exatamente "uma superfície na minha máquina, do meu usuário", e nada
    além disso.
    """
    _purge_expired()
    # Invalida códigos anteriores do MESMO usuário: só um vale por vez.
    for code in [c for c, p in _codes.items() if p.user_id == user_id]:
        _codes.pop(code, None)

    # secrets.randbelow para não gerar código previsível (random comum seria
    # semeado e adivinhável — é uma credencial, ainda que curta e efêmera).
    code = f"{secrets.randbelow(1_000_000):06d}"
    _codes[code] = _PendingCode(
        user_id=user_id,
        expires_at=time.time() + PAIRING_CODE_TTL_S,
        host_device_id=(host_device_id or None),
    )
    logger.info("[pair] código gerado para user=%s (ttl=%ss)", user_id, PAIRING_CODE_TTL_S)
    return code, PAIRING_CODE_TTL_S


def consume_pairing_code(code: str) -> UUID | None:
    """Valida e QUEIMA um código. Devolve o user_id dono, ou None.

    O código é de uso único: mesmo um pareamento que falhe depois (erro de
    banco) não permite reusá-lo — o usuário gera outro. É barato, e o contrário
    (código que sobrevive à tentativa) é uma janela aberta.

    Para também saber o hospedeiro, use ``consume_pairing_code_full``. Esta
    assinatura fica porque o pareamento de desktop/celular não tem vínculo e não
    precisa mudar.
    """
    resultado = consume_pairing_code_full(code)
    return None if resultado is None else resultado[0]


def consume_pairing_code_full(code: str) -> tuple[UUID, str | None] | None:
    """Como ``consume_pairing_code``, mas devolve ``(user_id, host_device_id)``."""
    _purge_expired()
    code = (code or "").strip()
    pending = _codes.get(code)
    if pending is None:
        logger.warning("[pair] código inválido ou expirado")
        return None
    _codes.pop(code, None)
    return pending.user_id, pending.host_device_id


def issue_device_token() -> tuple[str, str]:
    """Gera um token de device. Devolve (token_cru, hash) — só o hash é gravado."""
    token = secrets.token_urlsafe(32)
    return token, hash_device_token(token)


def hash_device_token(token: str) -> str:
    """sha256 hex do token.

    Sem salt de propósito: o token já é 32 bytes aleatórios, então não há
    dicionário a proteger, e o hash precisa ser determinístico para servir de
    índice de lookup no ``connected_device``.
    """
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def pending_code_count() -> int:
    """Quantos códigos vivos existem (usado por teste/diagnóstico)."""
    _purge_expired()
    return len(_codes)


def _reset_for_tests() -> None:
    _codes.clear()
