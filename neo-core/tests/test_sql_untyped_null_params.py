"""Parâmetro de SQL sem tipo em `IS NULL` quebra no asyncpg — e só no Postgres real.

`CASE WHEN :topic_embedding IS NULL ...` no `update_session` fazia o asyncpg
recusar a consulta (`AmbiguousParameterError: could not determine data type of
parameter $3`). O ramo só passou a rodar quando `topic_embedding` virou `vector`
(migration a0b1c2d3e4f5, 2026-09-19). De lá para cá: 3 turnos do desktop morreram
inteiros (sessão 389114d4) e, em silêncio, 23 cursores do conversation_recall, 11
skills ativas e 1 esclarecimento pendente não foram salvos — todo caminho que
passa por `update_session`.

A suíte não tem Postgres, então o teste de comportamento não pega isso. Este
confere a CLASSE do defeito no código-fonte: todo `:param IS [NOT] NULL` tem que
estar dentro de um CAST que dê tipo ao parâmetro.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted(
    p for p in [*ROOT.glob("db/*.py"), *ROOT.glob("core/**/*.py"), *ROOT.glob("modules/**/*.py")]
    if "__pycache__" not in p.parts
)

# `:nome IS NULL` / `:nome IS NOT NULL` sem CAST(...) em volta.
_UNTYPED = re.compile(r"(?<!AS text\)\s)(?<!\()\:(\w+)\s+IS\s+(NOT\s+)?NULL", re.I)


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_nenhum_parametro_sem_tipo_em_is_null(path: Path):
    src = path.read_text(encoding="utf-8")
    achados = [m.group(0) for m in _UNTYPED.finditer(src)]
    assert not achados, (
        f"{path.relative_to(ROOT)}: {achados} — use CAST(:param AS <tipo>) IS NULL; "
        "o asyncpg não infere o tipo de parâmetro solto em IS NULL."
    )


def test_update_session_tipa_o_embedding():
    src = (ROOT / "db" / "async_client.py").read_text(encoding="utf-8")
    assert "CAST(CAST(:topic_embedding AS text) AS vector)" in src
