"""Contador de tentativas de login — em memória, como os códigos de pareamento.

Em memória de propósito: o core é um processo só, a janela é de minutos, e um
restart no meio de um ataque de senha é indistinguível de janela nova (o atacante
não controla o restart). Persistir isso seria uma tabela para nada — mesmo
raciocínio de ``modules/devices/pairing.py``.

A chave é ``(usuário, ip)``: só o IP puniria o dono atrás do mesmo NAT do
atacante; só o usuário permitiria varrer senhas de um IP novo a cada tentativa.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from shared.config import get_settings


@dataclass
class _Janela:
    inicio: float
    tentativas: int = 0


_janelas: dict[tuple[str, str], _Janela] = {}


def _limites() -> tuple[int, int]:
    s = get_settings()
    return (
        int(getattr(s, "login_max_attempts", 8)),
        int(getattr(s, "login_attempt_window_s", 300)),
    )


def bloqueado(usuario: str, ip: str) -> int:
    """Segundos restantes de bloqueio, ou 0 se pode tentar."""
    maximo, janela_s = _limites()
    chave = ((usuario or "").lower(), ip or "?")
    janela = _janelas.get(chave)
    agora = time.time()
    if janela is None:
        return 0
    if agora - janela.inicio >= janela_s:
        _janelas.pop(chave, None)
        return 0
    if janela.tentativas < maximo:
        return 0
    return max(1, int(janela_s - (agora - janela.inicio)))


def registrar_falha(usuario: str, ip: str) -> None:
    _maximo, janela_s = _limites()
    chave = ((usuario or "").lower(), ip or "?")
    agora = time.time()
    janela = _janelas.get(chave)
    if janela is None or agora - janela.inicio >= janela_s:
        _janelas[chave] = _Janela(inicio=agora, tentativas=1)
        return
    janela.tentativas += 1


def limpar(usuario: str, ip: str) -> None:
    """Login certo zera o contador — senão o dono se tranca sozinho."""
    _janelas.pop(((usuario or "").lower(), ip or "?"), None)


def _reset_for_tests() -> None:
    _janelas.clear()
