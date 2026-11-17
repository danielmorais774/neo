"""`file_save` guarda sem prazo: a retenção não é escolha do modelo.

Session-a20f4722 (13/11): dos 3 MP3 entregues no chat, o 2º saiu `temp` (some em
7 dias) e os outros `perm`, sem o Daniel pedir nada. Escolha que o modelo troca
entre turnos vai para o código. Descartar depois é `file_set_retention`.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.files.executor import FilesExecutor
from core.files.tools import FILE_TOOLS


class _Store:
    def __init__(self) -> None:
        self.harvests: list[dict] = []

    async def harvest(self, path, *, name=None, retention="perm", user_id=None, session_id=None):
        self.harvests.append({"path": path, "retention": retention})
        return SimpleNamespace(id=uuid4(), name=name or path, size_bytes=10, retention=retention)


@pytest.mark.asyncio
async def test_temp_pedido_pelo_modelo_vira_perm():
    store = _Store()
    executor = FilesExecutor(store)  # type: ignore[arg-type]
    out = await executor.execute(
        "file_save", {"path": "/workspace/usb/loop10.mp3", "retention": "temp"}
    )
    assert store.harvests[0]["retention"] == "perm"
    assert out["retention"] == "perm"


def test_schema_do_file_save_nao_oferece_retencao():
    schema = next(t for t in FILE_TOOLS if t.name == "file_save")
    assert "retention" not in schema.parameters
    # A troca explícita continua existindo.
    assert any(t.name == "file_set_retention" for t in FILE_TOOLS)
