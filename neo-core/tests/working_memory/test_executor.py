"""WorkingMemoryExecutor — a fiação entre a tool call e o store.

O que erra aqui é a borda: argumento que chega como string em vez de bool, CSV
de links, erro de negócio que precisa virar mensagem acionável (e não stack) no
meio do loop cognitivo.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from core.working_memory.store import WorkingMemoryStore
from db.models.working_note import WorkingNote
from modules.working_memory import WorkingMemoryExecutor

USER = UUID("11111111-1111-1111-1111-111111111111")
SESSION = uuid4()


class _MemoryStore(WorkingMemoryStore):
    def __init__(self):
        super().__init__(db=None)
        self.rows: dict[tuple, WorkingNote] = {}

    async def _select_one(self, user_id, slug):
        return self.rows.get((user_id, slug))

    async def _select_index(self, user_id, limit):
        return [n for n in self.rows.values() if n.user_id == user_id]

    async def _persist(self, note):
        self.rows[(note.user_id, note.slug)] = note
        return note


@pytest.fixture
def executor():
    ex = WorkingMemoryExecutor(db=None, store=_MemoryStore())
    ex.set_context(USER, SESSION)
    return ex


@pytest.mark.asyncio
async def test_write_confirma_e_ensina_a_reabrir(executor):
    out = await executor.execute("wm_write", {
        "note": "Extrato Março",
        "content": "412 lançamentos, R$ 8.412,10 em débitos.",
    })
    assert out["note"] == "extrato-marco"
    assert out["entries"] == 1
    # A mensagem tem que empurrar o próximo turno para o wm_read.
    assert "wm_read('extrato-marco')" in out["message"]
    # E impedir que o modelo repita a anotação inteira ao usuário.
    assert "Não repita o conteúdo" in out["message"]


@pytest.mark.asyncio
async def test_write_carrega_a_sessao_do_turno(executor):
    await executor.execute("wm_write", {"note": "n", "content": "x"})
    note = executor._store.rows[(USER, "n")]
    assert note.session_id == SESSION
    assert note.origin == "model"


@pytest.mark.asyncio
async def test_links_chegam_como_csv(executor):
    await executor.execute("wm_write", {
        "note": "n", "content": "veio de [[file:abc]]",
        "links": "outra-nota, path:/workspace/p.py",
    })
    assert executor._store.rows[(USER, "n")].links == [
        "outra-nota", "path:/workspace/p.py", "file:abc",
    ]


@pytest.mark.asyncio
async def test_read_aceita_full_como_string(executor):
    for i in range(8):
        await executor.execute("wm_write", {"note": "n", "content": f"entrada {i}"})
    parcial = await executor.execute("wm_read", {"note": "n"})
    assert parcial["omitted_entries"] == 4
    # O modelo manda "true" (string) com frequência — tem que valer.
    inteiro = await executor.execute("wm_read", {"note": "n", "full": "true"})
    assert "entrada 0" in inteiro["body"]


@pytest.mark.asyncio
async def test_erro_de_negocio_vira_mensagem_nao_stack(executor):
    out = await executor.execute("wm_write", {"note": "!!!", "content": "x"})
    assert out["error"] == "bad_slug" and "extrato-marco" in out["message"]


@pytest.mark.asyncio
async def test_sem_usuario_recusa_sem_explodir():
    ex = WorkingMemoryExecutor(db=None, store=_MemoryStore())
    ex.set_context(None, None)
    assert (await ex.execute("wm_write", {"note": "n", "content": "x"}))["error"] == "no_user"
    assert (await ex.execute("wm_read", {"note": "n"}))["error"] == "no_user"


@pytest.mark.asyncio
async def test_tool_desconhecida_levanta(executor):
    with pytest.raises(ValueError):
        await executor.execute("wm_delete", {})
