"""WorkbenchExecutor — module executor (server-side) para as tools ``neo_*``.

Segue o mesmo contrato dos outros executores de módulo (``async def execute(name,
arguments) -> dict``), então plugga direto no ``ToolExecutor`` global via
``register_module_executor("workbench", ...)``. Delega ao :class:`WorkbenchSandbox`.
"""

from __future__ import annotations

import logging
from typing import Any

from core.workbench.sandbox import (
    SandboxResult,
    WorkbenchSandbox,
    WorkbenchUnavailableError,
)

logger = logging.getLogger(__name__)

# Teto de timeout no foreground: trabalho mais longo deve ir para background (F2).
# Fica ABAIXO do tool.timeout do schema neo_bash (150s) — assim o `timeout` do
# sandbox (wall = este + 10) mata o processo antes do global executor abortar o
# execute() por wait_for, evitando processo zumbi no container.
_MAX_FOREGROUND_TIMEOUT = 120


class WorkbenchExecutor:
    """Executor das tools do sandbox do Neo."""

    def __init__(self, sandbox: WorkbenchSandbox) -> None:
        self.sandbox = sandbox

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if tool_name == "neo_bash":
                return await self._bash(arguments)
            if tool_name == "neo_write_file":
                return await self._write_file(arguments)
            if tool_name == "neo_read_file":
                return await self._read_file(arguments)
            if tool_name == "neo_list_files":
                return await self._list_files(arguments)
            raise ValueError(f"Tool desconhecida no workbench: {tool_name}")
        except WorkbenchUnavailableError as e:
            logger.error("[Workbench] indisponível ao executar %s: %s", tool_name, e)
            return {
                "ok": False,
                "error": "O ambiente de execução do Neo está indisponível no momento.",
                "detail": str(e),
                "unavailable": True,
            }
        except ValueError as e:
            # Erro de caminho fora do workspace / tool inválida — devolve limpo.
            return {"ok": False, "error": str(e)}

    async def _bash(self, arguments: dict[str, Any]) -> dict[str, Any]:
        command = str(arguments.get("command") or "").strip()
        if not command:
            return {"ok": False, "error": "command vazio."}
        cwd = arguments.get("cwd")
        timeout = arguments.get("timeout")
        if timeout is not None:
            try:
                timeout = min(int(timeout), _MAX_FOREGROUND_TIMEOUT)
            except (TypeError, ValueError):
                timeout = None
        result = await self.sandbox.run(command, cwd=cwd, timeout=timeout)
        return self._result_payload(result, extra={"command": command})

    async def _write_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = str(arguments.get("path") or "").strip()
        if not path:
            return {"ok": False, "error": "path vazio."}
        content = arguments.get("content")
        if content is None:
            return {"ok": False, "error": "content ausente."}
        result = await self.sandbox.write_file(path, str(content))
        if not result.ok:
            return {"ok": False, "error": result.stderr or "falha ao escrever", "path": path}
        return {"ok": True, "path": self.sandbox.resolve_path(path), "bytes": len(str(content))}

    async def _read_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = str(arguments.get("path") or "").strip()
        if not path:
            return {"ok": False, "error": "path vazio."}
        result = await self.sandbox.read_file(path)
        if not result.ok:
            return {"ok": False, "error": result.stderr or "arquivo não encontrado", "path": path}
        return {
            "ok": True,
            "path": self.sandbox.resolve_path(path),
            "content": result.stdout,
            "truncated": result.truncated,
        }

    async def _list_files(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = arguments.get("path")
        result = await self.sandbox.list_files(path)
        if not result.ok:
            return {"ok": False, "error": result.stderr or "diretório não encontrado"}
        return {"ok": True, "listing": result.stdout}

    @staticmethod
    def _result_payload(result: SandboxResult, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": result.ok,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        if result.timed_out:
            payload["timed_out"] = True
            payload["error"] = "O comando excedeu o tempo limite e foi interrompido."
        if result.truncated:
            payload["truncated"] = True
        if extra:
            payload.update(extra)
        return payload
