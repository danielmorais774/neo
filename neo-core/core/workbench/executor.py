"""WorkbenchExecutor — module executor (server-side) para as tools ``neo_*``.

Segue o mesmo contrato dos outros executores de módulo (``async def execute(name,
arguments) -> dict``), então plugga direto no ``ToolExecutor`` global via
``register_module_executor("workbench", ...)``. Delega ao :class:`WorkbenchSandbox`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from core.workbench.sandbox import (
    SandboxResult,
    WorkbenchSandbox,
    WorkbenchUnavailableError,
)

logger = logging.getLogger(__name__)

# Um comando que está TRABALHANDO (CPU, I/O ou saída mudando) não morre por
# relógio, só por ficar parado. Na sessão c4f3f7de o teto antigo de 120s matou um
# ffmpeg com 89% do vídeo decodificado, duas vezes seguidas.
#
# - O `timeout` que o modelo passa vira a janela SEM PROGRESSO tolerada, presa em
#   [_STALL_MIN, _STALL_MAX] (padrão _STALL_DEFAULT).
# - _FOREGROUND_CEILING é a rede de segurança para laço infinito queimando CPU (que
#   parece progresso). Fica ABAIXO do tool.timeout do schema neo_bash — assim o
#   `timeout` do sandbox (wall = este + 10) mata o processo antes do global executor
#   abortar o execute() por wait_for, evitando processo zumbi no container.
_FOREGROUND_CEILING = 900
_STALL_DEFAULT = 90
_STALL_MIN = 30
_STALL_MAX = 300

# Quanto um comando pode prender o turno antes de virar job — quando existe quem o
# adote (ver `_bash`). "Não matar o que trabalha" sozinho virou "prender a
# conversa": na sessão 45bd5ad0 um OCR de 127 quadros segurou o turno 8 min.
_FOREGROUND_BUDGET = 120

# Teto do `neo_read_file` — o conteúdo entra INTEIRO no contexto do turno e é
# reenviado a cada round seguinte. Na sessão f33632b4 um .txt extraído de PDF
# voltou com 59,5 KB e o prompt chegou a 153 KB. Script e resultado cabem aqui;
# documento grande é trabalho do `deep_read` (docs/deep-read-plan.md §6).
_READ_FILE_MAX_BYTES = 32_000

#: Adota um `neo_bash` que passou do orçamento: recebe a task em curso e devolve o
#: payload que o modelo vê. Quem implementa é o JobToolExecutor, que conhece o
#: usuário e a sessão do turno (este executor é global e não conhece).
Adopter = Callable[..., Awaitable[dict[str, Any]]]


class WorkbenchExecutor:
    """Executor das tools do sandbox do Neo."""

    def __init__(self, sandbox: WorkbenchSandbox) -> None:
        self.sandbox = sandbox

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        adopt: Adopter | None = None,
        background_ceiling: int = 1800,
    ) -> dict[str, Any]:
        try:
            if tool_name == "neo_bash":
                return await self._bash(
                    arguments, adopt=adopt, background_ceiling=background_ceiling
                )
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

    async def _bash(
        self,
        arguments: dict[str, Any],
        *,
        adopt: Adopter | None = None,
        background_ceiling: int = 1800,
    ) -> dict[str, Any]:
        """Roda o comando; com ``adopt``, passa para background se demorar.

        Sem ``adopt`` (executor global, turno sem job manager) o comando fica em
        primeiro plano até ``_FOREGROUND_CEILING``. Com ``adopt``, passados
        ``_FOREGROUND_BUDGET`` segundos ainda trabalhando ele vira job: o turno
        responde na hora e o resultado chega pelo dispatcher. Nos dois casos, quem
        mata antes do teto é só o vigia de travamento.
        """
        command = str(arguments.get("command") or "").strip()
        if not command:
            return {"ok": False, "error": "command vazio."}
        cwd = arguments.get("cwd")
        stall = _STALL_DEFAULT
        if arguments.get("timeout") is not None:
            try:
                stall = max(_STALL_MIN, min(int(arguments["timeout"]), _STALL_MAX))
            except (TypeError, ValueError):
                pass
        # O `timeout` do coreutils é fixado na largada e não muda depois. Comando
        # adotável já nasce com o teto do JOB; quem limita o primeiro plano é o
        # orçamento abaixo, não um kill.
        ceiling = int(background_ceiling) if adopt is not None else _FOREGROUND_CEILING
        started = time.monotonic()
        run = asyncio.ensure_future(
            self.sandbox.run(command, cwd=cwd, timeout=ceiling, stall_timeout=stall)
        )
        try:
            done, _ = await asyncio.wait(
                {run}, timeout=_FOREGROUND_BUDGET if adopt is not None else None
            )
            if run not in done and adopt is not None:
                elapsed = int(time.monotonic() - started)
                try:
                    return await adopt(run, command=command, cwd=cwd, elapsed_s=elapsed)
                except Exception:  # noqa: BLE001 — sem job, o comando segue em primeiro plano
                    logger.exception("[Workbench] não consegui adotar o comando como job")
                    done, _ = await asyncio.wait(
                        {run}, timeout=max(0, _FOREGROUND_CEILING - elapsed)
                    )
                    if run not in done:
                        run.cancel()
                        return self._payload(
                            SandboxResult(exit_code=137, stdout="", stderr="", timed_out=True),
                            command, stall, _FOREGROUND_CEILING,
                        )
        except asyncio.CancelledError:
            # Turno cancelado ANTES da adoção: o `run` cancelado mata a árvore no
            # container. Depois da adoção quem cuida da task é o JobManager.
            run.cancel()
            raise
        return self._payload(run.result(), command, stall, ceiling)

    def _payload(
        self, result: SandboxResult, command: str, stall: int, ceiling: int
    ) -> dict[str, Any]:
        payload = self._result_payload(result, extra={"command": command})
        if result.stalled:
            payload["stalled"] = True
            payload["error"] = (
                f"O comando ficou {stall}s sem nenhum progresso (sem CPU, disco nem "
                "saída) e foi interrompido: parece travado, esperando entrada, rede "
                "ou um lock."
            )
        elif result.timed_out:
            payload["error"] = (
                "O comando ainda estava trabalhando quando bateu o teto de "
                f"{ceiling // 60} min e foi interrompido. Trabalho desse tamanho vai "
                "em neo_bash_background."
            )
        elif result.exit_code == 137:
            payload["error"] = (
                "O processo foi morto (SIGKILL) sem estourar prazo nenhum — o mais "
                "provável é falta de memória no sandbox. Processe em partes menores."
            )
        return payload

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
        result = await self.sandbox.read_file(path, max_bytes=_READ_FILE_MAX_BYTES)
        if not result.ok:
            return {"ok": False, "error": result.stderr or "arquivo não encontrado", "path": path}
        payload = {
            "ok": True,
            "path": self.sandbox.resolve_path(path),
            "content": result.stdout,
            "truncated": result.truncated,
        }
        if result.truncated:
            lidos = len(result.stdout.encode("utf-8"))
            payload["size_bytes"] = result.size_bytes
            payload["note"] = (
                f"O arquivo tem {result.size_bytes} bytes e só os primeiros ~{lidos} vieram. "
                "NÃO trate isto como o arquivo inteiro: leia o resto em partes com neo_bash "
                f"(ex.: `tail -c +{lidos + 1} <arquivo> | head -c 30000`) ou processe com um script. "
                "Se o objetivo é ANALISAR um documento grande (ou vários), não leia em partes: "
                "use deep_read, que lê tudo fora do seu contexto e devolve notas + síntese."
            )
        return payload

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
