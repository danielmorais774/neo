"""Session file logs API routes — read-only access to per-session Markdown files."""

import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/session-logs", tags=["session-logs"])

# Matches new format: session-2026-03-26T21-14-00-{uuid}.md
_TS_RE = re.compile(r"^session-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(.+)$")


def _logs_dir() -> Path:
    d = Path(os.environ.get("SESSION_LOGS_DIR", "/app/logs"))
    if not d.exists():
        d = Path(__file__).resolve().parent.parent.parent / "logs"
    return d


def _parse_filename(filename: str) -> dict:
    """Extract session_id and created_at from filename."""
    stem = Path(filename).stem  # strip .md
    m = _TS_RE.match(stem)
    if m:
        ts_raw = m.group(1)         # "2026-03-26T21-14-00"
        session_id = m.group(2)     # uuid part
        date_part, time_part = ts_raw.split("T")
        created_at = f"{date_part} {time_part.replace('-', ':')}"
    else:
        # Old format: session-{uuid}
        session_id = stem[len("session-"):] if stem.startswith("session-") else stem
        created_at = None
    return {"session_id": session_id, "created_at": created_at}


@router.get("")
async def list_session_logs():
    """List all session log files, sorted newest first."""
    d = _logs_dir()
    if not d.exists():
        return {"files": [], "total": 0}

    files = []
    for f in d.glob("session-*.md"):
        stat = f.stat()
        info = _parse_filename(f.name)
        files.append({
            "filename": f.name,
            "session_id": info["session_id"],
            "created_at": info["created_at"],
            "size_bytes": stat.st_size,
            "mtime": stat.st_mtime,
        })

    files.sort(key=lambda x: x["mtime"], reverse=True)
    return {"files": files, "total": len(files)}


@router.get("/{filename}")
async def get_session_log(filename: str):
    """Return the raw Markdown content of a session log file."""
    # Security: prevent path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "Filename inválido")
    if not filename.endswith(".md"):
        raise HTTPException(400, "Somente arquivos .md são servidos")

    path = _logs_dir() / filename
    if not path.exists():
        raise HTTPException(404, f"Arquivo não encontrado: {filename}")

    return PlainTextResponse(path.read_text(encoding="utf-8"))
