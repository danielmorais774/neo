"""WorkbenchSandbox — fala com o container ``neo-workbench`` via Docker Engine API.

O neo-core executa comandos no sandbox por ``docker exec`` sobre o socket unix
(``/var/run/docker.sock``, já montado no compose) — nunca pela rede. Usamos a
Engine API direto com httpx sobre UDS (sem dependência nova: httpx já é usado).

Isolamento e segurança são do container (compose): usuário não-root, cap_drop,
sem docker.sock lá dentro, limites de cpu/mem/pids e rede sem LAN. Aqui a defesa
é: (1) todo path é preso a ``/workspace``; (2) todo comando roda sob ``timeout``
do coreutils (teto de relógio), então processo travado morre dentro do
container — e, quando pedido, sob um vigia que mata o que ficou PARADO (sem CPU,
I/O nem saída) em vez de matar o que só está demorando; (3) saída é truncada
para não estourar o contexto do LLM.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import shlex
import struct
import time
import uuid
from dataclasses import dataclass
from typing import Callable

import httpx

from core.liveness import current_liveness, touch_progress

from core.workbench.user_paths import refusal_for

logger = logging.getLogger(__name__)

# Docker API sobre socket unix: o host da URL é ignorado, mas precisa ser válido.
_DOCKER_BASE_URL = "http://localhost"

# Todo processo nascido de um `run` herda esta variável com um id único. É por ela
# que o vigia acha a árvore inteira (timeout, sh, ffmpeg, netos) dentro do
# container, sem depender do PID do host que a Engine API devolve.
_EXEC_MARKER_ENV = "NEO_EXEC_ID"
_PROBE_INTERVAL_S = 5.0

# Roda DENTRO do sandbox (python3 está na imagem). Soma CPU
# (utime+stime+cutime+cstime) e I/O (rchar+wchar) de todo processo marcado e
# imprime "<processos> <ticks de cpu> <bytes de io>"; com "kill", manda SIGKILL
# neles. cutime/cstime entram para o CPU de um filho que já terminou não sumir da
# soma quando o pai o recolhe.
_PROBE_SCRIPT = r"""
import os, signal, sys
marker = ("%s=%s" % (sys.argv[1], sys.argv[2])).encode()
pids, cpu, io = [], 0, 0
for name in os.listdir("/proc"):
    if not name.isdigit():
        continue
    try:
        with open("/proc/%s/environ" % name, "rb") as f:
            if marker not in f.read().split(b"\0"):
                continue
        with open("/proc/%s/stat" % name, "rb") as f:
            stat = f.read()
    except OSError:
        continue
    pids.append(int(name))
    cpu += sum(int(x) for x in stat[stat.rindex(b")") + 2:].split()[11:15])
    try:
        with open("/proc/%s/io" % name, "rb") as f:
            io += sum(int(l.split()[1]) for l in f if l.startswith((b"rchar:", b"wchar:")))
    except OSError:
        pass
if sys.argv[3] == "kill":
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
print(len(pids), cpu, io)
"""


class WorkbenchUnavailableError(RuntimeError):
    """O container do sandbox não está acessível (parado, ausente, socket off)."""


@dataclass
class SandboxResult:
    """Resultado de um comando no sandbox."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False
    # Morto pelo vigia por ficar sem progresso (um caso particular de timed_out).
    stalled: bool = False
    # Tamanho real do arquivo lido (read_file/read_tail), quando conhecido.
    size_bytes: int | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class WorkbenchSandbox:
    """Cliente assíncrono para o container ``neo-workbench``."""

    def __init__(
        self,
        *,
        container: str = "neo-workbench",
        workspace_root: str = "/workspace",
        docker_socket: str = "/var/run/docker.sock",
        default_timeout: int = 60,
        max_output_bytes: int = 60_000,
        run_user: str = "neo",
    ) -> None:
        self.container = container
        self.workspace_root = workspace_root.rstrip("/") or "/workspace"
        self.default_timeout = default_timeout
        self.max_output_bytes = max_output_bytes
        self.run_user = run_user
        self._docker_socket = docker_socket

    # ------------------------------------------------------------------
    # Path safety
    # ------------------------------------------------------------------

    def resolve_path(self, path: str | None) -> str:
        """Resolve ``path`` para um caminho absoluto DENTRO de ``/workspace``.

        Aceita relativo (a partir de /workspace) ou absoluto sob /workspace.
        Rejeita qualquer coisa que escape (``..``, /etc, symlink-ish textual).
        A resolução é textual (não toca no FS do host) — combinada com o
        isolamento do container, é suficiente para o sandbox.
        """
        raw = (path or "").strip()
        if not raw or raw == ".":
            return self.workspace_root
        # Normaliza para absoluto sob o workspace.
        if raw.startswith("/"):
            candidate = raw
        else:
            candidate = f"{self.workspace_root}/{raw}"
        # Colapsa . e .. de forma puramente textual.
        parts: list[str] = []
        for seg in candidate.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(seg)
        resolved = "/" + "/".join(parts)
        root = self.workspace_root
        if resolved != root and not resolved.startswith(root + "/"):
            raise ValueError(
                f"Caminho fora do workspace do Neo ({root}): {path!r}"
            )
        return resolved

    # ------------------------------------------------------------------
    # Comandos
    # ------------------------------------------------------------------

    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        stall_timeout: int | None = None,
    ) -> SandboxResult:
        """Roda ``command`` (string de shell) no sandbox e devolve o resultado.

        ``timeout`` é o teto de relógio: o comando roda sob ``timeout`` do
        coreutils e, se estourar, é morto DENTRO do container (não deixa zumbi).
        ``stall_timeout`` liga o vigia de progresso: antes do teto, o comando só
        morre se passar esse tanto de segundos sem gastar CPU, fazer I/O nem
        escrever saída — se travou, não se está só demorando. A saída é truncada
        em ``max_output_bytes`` por fluxo.
        """
        # Ponto ÚNICO por onde passam o `neo_bash` do turno e o `sandbox_task`
        # em job — por isso a guarda mora aqui e não em cada chamador.
        recusa = refusal_for(command)
        if recusa is not None:
            logger.warning(
                "[Workbench] comando recusado: cita o disco do usuário — %.200s", command
            )
            return SandboxResult(exit_code=126, stdout="", stderr=recusa)

        secs = int(timeout or self.default_timeout)
        workdir = self.resolve_path(cwd) if cwd else self.workspace_root
        # `timeout --signal=KILL N sh -lc '<command>'` — garante o kill no host
        # do processo. cd para o workdir primeiro (Docker WorkingDir também é
        # setado, mas cwd validado é a fonte da verdade).
        wrapped = f"cd {shlex.quote(workdir)} && {command}"
        cmd = [
            "timeout",
            "--signal=KILL",
            str(secs),
            "sh",
            "-lc",
            wrapped,
        ]
        marker = uuid.uuid4().hex
        # Sem vigia pedido e sem turno esperando sinal de vida (job em background,
        # por exemplo), não vale gastar um probe a cada poucos segundos.
        monitor = None
        if stall_timeout is not None or current_liveness() is not None:
            monitor = _ProgressMonitor(self, marker, stall_timeout=stall_timeout)
        started = time.monotonic()
        try:
            result = await self._exec(
                cmd,
                workdir=workdir,
                env={**(env or {}), _EXEC_MARKER_ENV: marker},
                wall_timeout=secs + 10,
                monitor=monitor,
            )
        except asyncio.CancelledError:
            # Stop no desktop ou cliente caiu: cancelar a task não mata o `docker
            # exec`, e o processo seguiria no container até o teto — a sessão
            # 45bd5ad0 deixou um OCR rodando minutos depois do Stop. Mata a árvore
            # marcada antes de propagar.
            await asyncio.shield(self._probe(marker, kill=True))
            raise
        # coreutils timeout: 124 = expirou; 137 = morto (SIGKILL, nosso sinal).
        if result.exit_code in (124, 137):
            if monitor is not None and monitor.stalled:
                result.timed_out = True
                result.stalled = True
            # Pelo relógio, não pela saída vazia: o ffmpeg imprime progresso antes
            # de morrer, e um 137 rápido sem saída é falta de memória, não prazo.
            elif time.monotonic() - started >= secs:
                result.timed_out = True
        return result

    async def write_file(self, path: str, content: str) -> SandboxResult:
        """Escreve ``content`` (texto) em ``path`` dentro do workspace.

        Passa o conteúdo como base64 (alfabeto seguro no shell) e decodifica no
        container — evita qualquer problema de escaping com o conteúdo do arquivo.
        """
        target = self.resolve_path(path)
        parent = target.rsplit("/", 1)[0] or "/"
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        script = (
            f"mkdir -p {shlex.quote(parent)} && "
            f"printf %s {shlex.quote(b64)} | base64 -d > {shlex.quote(target)}"
        )
        return await self._exec(["sh", "-c", script], workdir=self.workspace_root)

    async def read_file(self, path: str, *, max_bytes: int | None = None) -> SandboxResult:
        """Lê ``path`` (texto). Trunca em ``max_bytes`` (default: max_output_bytes)."""
        target = shlex.quote(self.resolve_path(path))
        cap = int(max_bytes or self.max_output_bytes)
        # head -c limita no container. O tamanho vem junto porque o corte do head
        # nunca passava pelo `_truncate` do _exec: um arquivo de 64 KB voltava com
        # 60 KB e `truncated=False` (session-1fcaa440, a transcrição do OCR).
        result = await self._exec(
            ["sh", "-c", f"wc -c < {target} && head -c {cap} {target}"],
            workdir=self.workspace_root,
        )
        if result.exit_code != 0:
            return result
        tamanho, _, corpo = result.stdout.partition("\n")
        try:
            result.size_bytes = int(tamanho.strip())
        except ValueError:
            return result
        result.stdout = corpo
        result.truncated = result.truncated or result.size_bytes > cap
        return result

    async def read_tail(self, path: str, *, max_bytes: int | None = None) -> SandboxResult:
        """O FIM de ``path`` — o que importa num log ("como está indo").

        ``read_file`` devolve o começo, que num log de job longo é justamente a
        parte velha. ``truncated`` diz se havia mais antes do trecho devolvido.
        """
        target = shlex.quote(self.resolve_path(path))
        cap = int(max_bytes or self.max_output_bytes)
        result = await self._exec(
            ["sh", "-c", f"wc -c < {target} && tail -c {cap} {target}"],
            workdir=self.workspace_root,
        )
        if result.exit_code != 0:
            return result
        tamanho, _, corpo = result.stdout.partition("\n")
        try:
            cortado = int(tamanho.strip()) > cap
        except ValueError:
            return result
        if cortado:
            # O corte por bytes cai no meio de uma linha: a primeira é lixo.
            corpo = corpo.split("\n", 1)[-1]
        result.stdout = corpo
        result.truncated = result.truncated or cortado
        return result

    async def list_files(self, path: str | None = None) -> SandboxResult:
        """Lista ``path`` (default: raiz do workspace) com ``ls -la``."""
        target = self.resolve_path(path)
        return await self._exec(["ls", "-la", "--", target], workdir=self.workspace_root)

    async def health(self) -> bool:
        """True se o container está rodando e responde a um exec trivial."""
        try:
            info = await self._inspect_container()
            if not info.get("State", {}).get("Running"):
                return False
            res = await self._exec(["true"], workdir=self.workspace_root, wall_timeout=10)
            return res.exit_code == 0
        except Exception as e:  # noqa: BLE001
            logger.warning("[Workbench] health check failed: %s", e)
            return False

    async def _probe(self, marker: str, *, kill: bool = False) -> tuple[int, int, int] | None:
        """Amostra (ou mata, com ``kill``) a árvore de processos de um `run`.

        Devolve ``(processos, ticks de cpu, bytes de io)``, ou ``None`` quando não
        deu para medir — o vigia nunca derruba o comando que está vigiando.
        """
        try:
            res = await self._exec(
                [
                    "python3",
                    "-c",
                    _PROBE_SCRIPT,
                    _EXEC_MARKER_ENV,
                    marker,
                    "kill" if kill else "sample",
                ],
                workdir=self.workspace_root,
                wall_timeout=15,
            )
            pids, cpu, io = (int(x) for x in res.stdout.split())
        except Exception as e:  # noqa: BLE001
            logger.debug("[Workbench] probe falhou (exec %s): %s", marker, e)
            return None
        return pids, cpu, io

    # ------------------------------------------------------------------
    # Docker Engine API
    # ------------------------------------------------------------------

    async def _client(self) -> httpx.AsyncClient:
        # Um transporte por client: sair do `async with` fecha o transporte, e um
        # compartilhado derrubava qualquer exec em curso — o stream de um comando
        # morria quando o probe do vigia (ou um job em paralelo) terminava.
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=self._docker_socket),
            base_url=_DOCKER_BASE_URL,
            timeout=None,
        )

    async def _inspect_container(self) -> dict:
        async with await self._client() as client:
            resp = await client.get(f"/containers/{self.container}/json")
            if resp.status_code == 404:
                raise WorkbenchUnavailableError(f"container '{self.container}' não existe")
            resp.raise_for_status()
            return resp.json()

    async def _exec(
        self,
        cmd: list[str],
        *,
        workdir: str,
        env: dict[str, str] | None = None,
        wall_timeout: int = 90,
        monitor: _ProgressMonitor | None = None,
    ) -> SandboxResult:
        """Cria e roda um exec, devolvendo stdout/stderr/exit_code."""
        env_list = [f"{k}={v}" for k, v in (env or {}).items()]
        create_body = {
            "AttachStdout": True,
            "AttachStderr": True,
            "AttachStdin": False,
            "Tty": False,
            "Cmd": cmd,
            "WorkingDir": workdir,
            "User": self.run_user,
            "Env": env_list,
        }
        try:
            async with await self._client() as client:
                create = await client.post(
                    f"/containers/{self.container}/exec", json=create_body
                )
                if create.status_code == 404:
                    raise WorkbenchUnavailableError(
                        f"container '{self.container}' indisponível para exec"
                    )
                create.raise_for_status()
                exec_id = create.json()["Id"]

                async def _run() -> bytes:
                    async with client.stream(
                        "POST",
                        f"/exec/{exec_id}/start",
                        json={"Detach": False, "Tty": False},
                        headers={"Content-Type": "application/json"},
                    ) as stream:
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in stream.aiter_bytes():
                            chunks.append(chunk)
                            total += len(chunk)
                            if monitor is not None:
                                monitor.saw_output()
                            # Guarda-chuva: para de ler bem além do cap por fluxo.
                            if total > self.max_output_bytes * 4:
                                break
                        return b"".join(chunks)

                watcher = asyncio.create_task(monitor.watch()) if monitor is not None else None
                try:
                    raw = await asyncio.wait_for(_run(), timeout=wall_timeout)
                except asyncio.TimeoutError:
                    return SandboxResult(
                        exit_code=124, stdout="", stderr="", timed_out=True
                    )
                finally:
                    if watcher is not None:
                        watcher.cancel()

                stdout, stderr = _demux(raw)
                info = await client.get(f"/exec/{exec_id}/json")
                exit_code = 1
                if info.status_code == 200:
                    exit_code = info.json().get("ExitCode")
                    if exit_code is None:
                        exit_code = 1
        except (httpx.ConnectError, httpx.ReadError, FileNotFoundError) as e:
            raise WorkbenchUnavailableError(f"socket do Docker inacessível: {e}") from e

        out, out_trunc = _truncate(stdout, self.max_output_bytes)
        err, err_trunc = _truncate(stderr, self.max_output_bytes)
        return SandboxResult(
            exit_code=int(exit_code),
            stdout=out,
            stderr=err,
            truncated=out_trunc or err_trunc,
        )


class _ProgressMonitor:
    """Decide, por evidência, se um comando em curso travou.

    Progresso é qualquer mudança observável: saída chegando pelo stream, ou a
    amostra (processos, CPU, I/O) da árvore marcada diferente da anterior. Todo
    avanço vira também sinal de vida do turno (``touch_progress``), que é o que
    mantém o desktop esperando. Sem ``stall_timeout`` o vigia só alimenta esse
    sinal e nunca mata.

    Um laço infinito queimando CPU conta como progresso — quem pega esse caso é
    o teto de relógio do ``run``.
    """

    def __init__(
        self,
        sandbox: WorkbenchSandbox,
        marker: str,
        *,
        stall_timeout: float | None,
        clock: Callable[[], float] = time.monotonic,
        interval: float = _PROBE_INTERVAL_S,
    ) -> None:
        self._sandbox = sandbox
        self._marker = marker
        self._stall_timeout = stall_timeout
        self._clock = clock
        self._interval = interval
        self._last_progress = clock()
        self._last_sample: tuple[int, int, int] | None = None
        self.stalled = False

    def saw_output(self) -> None:
        self._progress()

    def observe(self, sample: tuple[int, int, int] | None) -> bool:
        """Registra uma amostra do probe; True quando o comando deve morrer."""
        if sample is None:
            # Probe falhou: não é evidência de avanço nem de parada. Não mata por
            # isso — o teto de relógio continua valendo.
            return False
        if sample != self._last_sample:
            self._last_sample = sample
            self._progress()
            return False
        if self._stall_timeout is None:
            return False
        return self._clock() - self._last_progress >= self._stall_timeout

    async def watch(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            if not self.observe(await self._sandbox._probe(self._marker)):
                continue
            self.stalled = True
            logger.warning(
                "[Workbench] comando sem progresso há %ss — matando a árvore (exec %s)",
                self._stall_timeout,
                self._marker,
            )
            await self._sandbox._probe(self._marker, kill=True)
            return

    def _progress(self) -> None:
        self._last_progress = self._clock()
        touch_progress()


def _demux(raw: bytes) -> tuple[str, str]:
    """Separa o stream multiplexado do Docker (Tty=false) em (stdout, stderr).

    Cada frame: header de 8 bytes [tipo, 0,0,0, tamanho(4, big-endian)] + payload.
    tipo 0/1 = stdout, 2 = stderr. Se o header não for um frame válido (stream não
    multiplexado — raro, já que sempre usamos Tty=false), trata o resto como stdout
    cru em vez de interpretar bytes arbitrários como tamanho de frame.
    """
    stdout = bytearray()
    stderr = bytearray()
    i = 0
    n = len(raw)
    while i + 8 <= n:
        stream_type = raw[i]
        if stream_type > 2 or raw[i + 1 : i + 4] != b"\x00\x00\x00":
            # Não parece um header de frame → o restante é saída crua.
            stdout.extend(raw[i:])
            i = n
            break
        size = struct.unpack(">I", raw[i + 4 : i + 8])[0]
        i += 8
        payload = raw[i : i + size]
        i += size
        if stream_type == 2:
            stderr.extend(payload)
        else:
            stdout.extend(payload)
    if i < n:
        # Sobra menor que um header (frame parcial) — anexa como stdout.
        stdout.extend(raw[i:])
    return (
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


def _truncate(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    clipped = encoded[:max_bytes].decode("utf-8", "ignore")
    return clipped + "\n…[saída truncada]", True
