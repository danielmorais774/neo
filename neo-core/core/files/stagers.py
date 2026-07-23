"""Stagers — materializam um blob no ambiente onde ele vai ser usado (§4).

``file_stage(file_id, target=...)`` é a "função que traz o arquivo pro ambiente".
A interface nasce plural de propósito: a F1 implementa só ``workbench``, mas
browser-agent (volume de perfil) e gateway (corpo de upload HTTP) entram como
novos Stagers sem mexer na tool nem no store.

O sandbox monta o store **read-only** em ``/files`` — o stage é um `cp` de
`/files/blobs/<sha>` para `/workspace/files/<nome>`, feito DENTRO do sandbox.
O core não precisa escrever no workspace (que monta ro nele), e o sandbox não
consegue corromper o store.
"""

from __future__ import annotations

import logging
import shlex
from pathlib import Path
from typing import Any, Protocol

from db.models.stored_file import StoredFile

logger = logging.getLogger(__name__)


class Stager(Protocol):
    """Materializa ``entry`` no ambiente e devolve a referência utilizável."""

    async def stage(self, entry: StoredFile, blob: Path, *, name: str) -> dict[str, Any]:
        ...


class WorkbenchStager:
    """Copia o blob para ``/workspace/files/<nome>`` via `cp` no sandbox.

    ``store_mount`` é onde o volume do store aparece DENTRO do sandbox (ro).
    """

    def __init__(
        self,
        sandbox: Any,
        *,
        store_mount: str = "/files",
        dest_subdir: str = "files",
    ) -> None:
        self._sandbox = sandbox
        self._store_mount = store_mount.rstrip("/")
        self._dest_subdir = dest_subdir.strip("/")

    async def stage(self, entry: StoredFile, blob: Path, *, name: str) -> dict[str, Any]:
        # Path do blob na ótica do SANDBOX (não a do core).
        sha = entry.sha256
        src = f"{self._store_mount}/blobs/{sha[:2]}/{sha}"
        dest_dir = f"{self._sandbox.workspace_root}/{self._dest_subdir}"
        dest = f"{dest_dir}/{name}"
        command = f"mkdir -p {shlex.quote(dest_dir)} && cp {shlex.quote(src)} {shlex.quote(dest)}"
        result = await self._sandbox.run(command, timeout=60)
        if result.exit_code != 0:
            stderr = (result.stderr or "").strip()[:300]
            raise RuntimeError(
                f"falha ao materializar '{entry.name}' no sandbox (exit {result.exit_code}): {stderr}"
            )
        logger.info("[files] staged %s → %s", entry.id, dest)
        return {
            "path": dest,
            "note": (
                f"Arquivo disponível em {dest} dentro do SEU sandbox. Manipule com "
                "neo_bash (ex.: wc -l, python). O conteúdo não passou pelo contexto."
            ),
        }
