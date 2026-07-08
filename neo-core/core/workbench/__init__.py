"""Workbench — o sandbox de execução do próprio Neo (a "bash do Neo").

Quando não existe tool específica para um pedido, o núcleo cognitivo delega para
o Workbench: um container irmão (``neo-workbench``) onde o Neo escreve scripts,
roda comandos e monta projetos, isolado da máquina do usuário e da LAN. O
neo-core fala com ele via ``docker exec`` (o socket já está montado) — nada
trafega pela rede entre os dois.

Ver ``docs/workbench-background-jobs-plan.md``.
"""

from core.workbench.sandbox import (
    SandboxResult,
    WorkbenchSandbox,
    WorkbenchUnavailableError,
)
from core.workbench.executor import WorkbenchExecutor
from core.workbench.tools import WORKBENCH_TOOLS

__all__ = [
    "SandboxResult",
    "WorkbenchSandbox",
    "WorkbenchUnavailableError",
    "WorkbenchExecutor",
    "WORKBENCH_TOOLS",
]
