"""App logs route — read-only access to the app.log file written by neo-core."""

import os
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/app-logs", tags=["app-logs"])

# O app.log é escrito por `tee -a` no entrypoint, recebendo stdout/stderr de
# vários processos pelo mesmo pipe: escrita maior que PIPE_BUF (4KB) intercala e
# corta caractere multibyte no meio, então o arquivo NÃO é utf-8 válido (era um
# 500 em todo GET). Daí errors="replace". E ele chega a centenas de MB, então
# lemos só a cauda do fim do arquivo em vez de carregar tudo na RAM.
_CHUNK = 64 * 1024
_MAX_TAIL_BYTES = 8 * 1024 * 1024


def _logs_dir() -> Path:
    d = Path(os.environ.get("SESSION_LOGS_DIR", "/app/logs"))
    if not d.exists():
        d = Path(__file__).resolve().parent.parent.parent / "logs"
    return d


def _log_file() -> Path:
    return _logs_dir() / "app.log"


def _read_tail(path: Path, tail: int) -> str:
    """Lê as últimas `tail` linhas lendo do fim do arquivo para trás."""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        pos = end
        newlines = 0
        blocks: list[bytes] = []

        while pos > 0 and newlines <= tail and (end - pos) < _MAX_TAIL_BYTES:
            size = min(_CHUNK, pos)
            pos -= size
            f.seek(pos)
            block = f.read(size)
            newlines += block.count(b"\n")
            blocks.append(block)

    data = b"".join(reversed(blocks))
    lines = data.decode("utf-8", errors="replace").splitlines()
    # O primeiro bloco lido pode começar no meio de uma linha; descarta o pedaço.
    if pos > 0 and lines:
        lines = lines[1:]
    return "\n".join(lines[-tail:])


@router.get("")
async def get_app_logs(tail: int = Query(default=500, ge=1, le=10000)) -> PlainTextResponse:
    """Return the last `tail` lines from the app.log file."""
    path = _log_file()
    if not path.exists():
        return PlainTextResponse("No app.log file found")

    try:
        return PlainTextResponse(_read_tail(path, tail))
    except Exception as e:
        raise HTTPException(500, f"Error reading app.log: {e}")
