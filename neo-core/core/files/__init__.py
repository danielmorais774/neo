"""File Store — entrada, catálogo e uso de arquivos pelo Neo.

Ver docs/file-store-plan.md. Use ``build_file_store`` para montar o store com o
stager do workbench já registrado.
"""

from __future__ import annotations

from typing import Any

from core.files.executor import FilesExecutor
from core.files.gc import gc_loop
from core.files.stagers import WorkbenchStager
from core.files.store import FileStore, FileStoreError, safe_name
from core.files.tools import CUPOM_TOOL_NAMES, CUPOM_TOOLS, FILE_TOOL_NAMES, FILE_TOOLS

__all__ = [
    "build_file_store",
    "FileStore",
    "FileStoreError",
    "FilesExecutor",
    "FILE_TOOLS",
    "CUPOM_TOOLS",
    "CUPOM_TOOL_NAMES",
    "FILE_TOOL_NAMES",
    "WorkbenchStager",
    "gc_loop",
    "safe_name",
]


def build_file_store(*, db: Any, settings: Any, sandbox: Any = None) -> FileStore:
    """Monta o FileStore a partir das settings; registra o stager do workbench
    quando há sandbox (sem sandbox, ``file_stage`` responde 'target não suportado')."""
    stagers: dict[str, Any] = {}
    if sandbox is not None:
        stagers["workbench"] = WorkbenchStager(
            sandbox, store_mount=settings.neo_files_sandbox_mount
        )
    return FileStore(
        db=db,
        root=settings.neo_files_root,
        workspace_root=settings.workbench_workspace_root,
        max_file_bytes=settings.neo_files_max_file_mb * 1024 * 1024,
        quota_bytes=int(settings.neo_files_quota_gb * 1024 * 1024 * 1024),
        read_cap_chars=settings.neo_files_read_cap_chars,
        ttl_days=settings.neo_files_ttl_days,
        stagers=stagers,
    )
