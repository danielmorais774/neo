"""A interface do corpo: onde o Claude Code roda (docs/claude-code-delegation-plan.md §2).

O orquestrador não sabe se o Claude está no Mac do Daniel ou num container. Ele
fala com estas operações, e cada corpo as implementa do seu jeito. O corpo não
interpreta o stream: só lança, entrega bytes e para — o binário é o mesmo nos
dois lugares, então quem lê é o ``stream.py``, uma vez só.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class CorpoIndisponivel(Exception):
    """O corpo não respondeu. ``fora_do_ar`` = vai voltar sozinho (Mac dormindo)."""

    def __init__(self, motivo: str, *, fora_do_ar: bool = False) -> None:
        super().__init__(motivo)
        self.fora_do_ar = fora_do_ar


@dataclass
class Saude:
    ok: bool
    motivo: str = ""
    versao: str = ""
    logado: bool = False
    metodo: str = ""
    plano: str = ""
    #: A pasta pedida, conferida no Mac: True/False, ou None quando não se
    #: perguntou (ou o desktop é antigo e não sabe responder).
    pasta_ok: bool | None = None
    pasta_motivo: str = ""


@dataclass
class PedidoDeRodada:
    """O que o orquestrador manda lançar. As flags já vêm montadas."""

    run_id: str
    cwd: str
    prompt: str
    flags: list[str]
    confirmar: dict[str, Any] = field(default_factory=dict)


@dataclass
class Lancamento:
    run_id: str
    cwd: str
    pid: int | None = None


@dataclass
class Leitura:
    pedaco: str
    offset: int
    vivo: bool
    codigo_saida: int | None = None


class Corpo(Protocol):
    nome: str

    async def saude(self, pasta: str = "") -> Saude: ...

    async def lancar(self, pedido: PedidoDeRodada) -> Lancamento: ...

    async def ler(self, run_id: str, offset: int) -> Leitura: ...

    async def parar(self, run_id: str) -> None: ...

    async def recolher(self, cwd: str, caminhos: list[str]) -> list[dict[str, Any]]: ...

    async def limpar(self, run_id: str) -> None: ...

    def flags_de_permissao(self, liberadas: list[str]) -> list[str]: ...
