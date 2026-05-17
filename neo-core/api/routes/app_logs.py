"""App logs route — read-only access to the app.log file written by neo-core."""

import os
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/app-logs", tags=["app-logs"])


def _logs_dir() -> Path:
    d = Path(os.environ.get("SESSION_LOGS_DIR", "/app/logs"))
    if not d.exists():
        d = Path(__file__).resolve().parent.parent.parent / "logs"
    return d


def _log_file() -> Path:
    return _logs_dir() / "app.log"


@router.get("")
async def get_app_logs(tail: int = Query(default=500, ge=1, le=10000)) -> PlainTextResponse:
    """Return the last `tail` lines from the app.log file."""
    path = _log_file()
    if not path.exists():
        return PlainTextResponse("No app.log file found")

    try:
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()

        # Return only the last `tail` lines
        last_lines = lines[-tail:] if tail < len(lines) else lines
        return PlainTextResponse("\n".join(last_lines))
    except Exception as e:
        raise HTTPException(500, f"Error reading app.log: {e}")