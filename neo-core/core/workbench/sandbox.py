"""WorkbenchSandbox — fala com o container ``neo-workbench`` via Docker Engine API.

O neo-core executa comandos no sandbox por ``docker exec`` sobre o socket unix
(``/var/run/docker.sock``, já montado no compose) — nunca pela rede. Usamos a
Engine API direto com httpx sobre UDS (sem dependência nova: httpx já é usado).

Isolamento e segurança são do container (compose): usuário não-root, cap_drop,
sem docker.sock lá dentro, limites de cpu/mem/pids e rede sem LAN. Aqui a defesa
é: (1) todo path é preso a ``/workspace``; (2) todo comando roda sob ``timeout``
do coreutils, então processo travado morre dentro do container; (3) saída é
truncada para não estourar o contexto do LLM.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import shlex
import struct
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Docker API sobre socket unix: o host da URL é ignorado, mas precisa ser válido.
_DOCKER_BASE_URL = "http://localhost"


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
        self._transport = httpx.AsyncHTTPTransport(uds=docker_socket)

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
    ) -> SandboxResult:
        """Roda ``command`` (string de shell) no sandbox e devolve o resultado.

        O comando roda sob ``timeout`` do coreutils: se estourar, o processo é
        morto DENTRO do container (não deixa zumbi). A saída é truncada em
        ``max_output_bytes`` por fluxo.
        """
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
        result = await self._exec(cmd, workdir=workdir, env=env, wall_timeout=secs + 10)
        # coreutils timeout: 124 = expirou; 137 = morto (SIGKILL, nosso sinal).
        if result.exit_code in (124, 137) and not result.stdout and not result.stderr:
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
        target = self.resolve_path(path)
        cap = int(max_bytes or self.max_output_bytes)
        # head -c limita no container; o truncamento textual final é do _exec.
        script = f"head -c {cap} {shlex.quote(target)}"
        return await self._exec(["sh", "-c", script], workdir=self.workspace_root)

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

    # ------------------------------------------------------------------
    # Docker Engine API
    # ------------------------------------------------------------------

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, base_url=_DOCKER_BASE_URL, timeout=None)

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
                            # Guarda-chuva: para de ler bem além do cap por fluxo.
                            if total > self.max_output_bytes * 4:
                                break
                        return b"".join(chunks)

                try:
                    raw = await asyncio.wait_for(_run(), timeout=wall_timeout)
                except asyncio.TimeoutError:
                    return SandboxResult(
                        exit_code=124, stdout="", stderr="", timed_out=True
                    )

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
