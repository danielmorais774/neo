"""WorkingMemoryStore — upsert, carimbo, truncagem, links, índice, GC.

Sem Postgres: a subclasse abaixo troca os três métodos finos de banco
(``_select_one``/``_select_index``/``_persist``) por um dict em memória. Toda a
lógica que erra (slug, entrada datada, poda do corpo, ordenação do índice) é a
de cima, e é ela que está sob teste. Ver docs/working-memory-plan.md §10.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest

from core.working_memory.store import (
    WorkingMemoryError,
    WorkingMemoryStore,
    derive_summary,
    extract_links,
    slugify,
)
from db.models.working_note import WorkingNote

USER = UUID("11111111-1111-1111-1111-111111111111")
SESSION = UUID("22222222-2222-2222-2222-222222222222")


class MemoryStore(WorkingMemoryStore):
    """Store com banco em memória — mesma lógica, sem I/O."""

    def __init__(self, **kw):
        super().__init__(db=None, **kw)
        self.rows: dict[tuple[UUID, str], WorkingNote] = {}

    async def _select_one(self, user_id, slug):
        return self.rows.get((user_id, slug))

    async def _select_index(self, user_id, limit):
        rows = [
            n for n in self.rows.values()
            if n.user_id == user_id and n.status == "active"
        ]
        rows.sort(key=lambda n: n.updated_at, reverse=True)
        return rows[:limit]

    async def _persist(self, note):
        self.rows[(note.user_id, note.slug)] = note
        return note


@pytest.fixture
def store():
    return MemoryStore()


# ── helpers puros ───────────────────────────────────────────────────────────

def test_slugify_normaliza_acento_e_espaco():
    assert slugify("Extrato Nubank Março") == "extrato-nubank-marco"
    assert slugify("  ANÁLISE//gastos  ") == "analise-gastos"
    assert slugify("já---feito") == "ja-feito"
    assert slugify("!!!") == ""


def test_slugify_variacoes_do_mesmo_nome_colidem_de_proposito():
    # É o que impede o caderno de virar confete: o modelo escreve o slug à mão
    # em cada turno e não acerta a grafia duas vezes seguidas.
    assert slugify("extrato-marco") == slugify("Extrato Março") == "extrato-marco"


def test_extract_links_ordem_sem_repetir():
    text = "veio de [[file:abc]] e liga [[outra-nota]] e [[file:abc]] de novo"
    assert extract_links(text) == ["file:abc", "outra-nota"]


def test_derive_summary_limpa_markdown_e_pula_bullet():
    # O resumo ocupa o prompt em TODO turno: nada de `**`, `#` ou bullet solto.
    assert derive_summary("**412 lançamentos** apurados\n- detalhe") == "412 lançamentos apurados"
    assert derive_summary("- só bullets\n- mesmo assim") == "só bullets - mesmo assim"
    assert derive_summary("") == ""


# ── escrita ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_cria_nota_com_cabecalho_e_entrada_datada(store):
    now = datetime(2026, 7, 31, 17, 41)  # 14:41 em Brasília
    note = await store.write(
        user_id=USER, session_id=SESSION, slug="extrato-marco",
        content="412 lançamentos, R$ 8.412,10 em débitos.",
        title="Extrato março", now=now,
    )
    assert note.slug == "extrato-marco"
    assert note.entry_count == 1
    assert note.body.startswith("# Extrato março")
    # O horário fica DENTRO do markdown, não só na coluna — é o que o modelo lê.
    assert "## 31/07 14:41" in note.body
    assert "412 lançamentos" in note.body
    assert note.summary == "412 lançamentos, R$ 8.412,10 em débitos."
    assert note.expires_at == now + timedelta(days=store.ttl_days)


@pytest.mark.asyncio
async def test_write_no_mesmo_slug_acrescenta_entrada_nao_duplica_nota(store):
    await store.write(user_id=USER, slug="extrato-marco", content="primeira apuração",
                      now=datetime(2026, 7, 31, 17, 20))
    note = await store.write(user_id=USER, slug="Extrato Março", content="ajuste dos estornos",
                             now=datetime(2026, 7, 31, 17, 41))
    assert len(store.rows) == 1
    assert note.entry_count == 2
    assert "primeira apuração" in note.body and "ajuste dos estornos" in note.body
    assert note.body.count("## ") == 2
    # O resumo do índice acompanha a ÚLTIMA conclusão.
    assert note.summary == "ajuste dos estornos"


@pytest.mark.asyncio
async def test_write_replace_descarta_historico(store):
    await store.write(user_id=USER, slug="n", content="valor errado")
    note = await store.write(user_id=USER, slug="n", content="valor certo", mode="replace")
    assert note.entry_count == 1
    assert "valor errado" not in note.body and "valor certo" in note.body


@pytest.mark.asyncio
async def test_write_acumula_links_do_conteudo_e_do_argumento(store):
    note = await store.write(
        user_id=USER, slug="n", content="veio de [[file:abc123]]",
        links=["analise-gastos"],
    )
    assert note.links == ["analise-gastos", "file:abc123"]
    note = await store.write(user_id=USER, slug="n", content="script em [[path:/workspace/p.py]]")
    assert note.links == ["analise-gastos", "file:abc123", "path:/workspace/p.py"]


@pytest.mark.asyncio
async def test_write_recusa_entrada_invalida(store):
    with pytest.raises(WorkingMemoryError) as e:
        await store.write(user_id=None, slug="n", content="x")
    assert e.value.code == "no_user"
    with pytest.raises(WorkingMemoryError) as e:
        await store.write(user_id=USER, slug="!!!", content="x")
    assert e.value.code == "bad_slug"
    with pytest.raises(WorkingMemoryError) as e:
        await store.write(user_id=USER, slug="n", content="   ")
    assert e.value.code == "empty"
    with pytest.raises(WorkingMemoryError) as e:
        await store.write(user_id=USER, slug="n", content="x", mode="upsert")
    assert e.value.code == "bad_mode"


# ── truncagem ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_truncagem_derruba_as_entradas_mais_antigas():
    # Cortar pelo fim perderia justamente a conclusão que o próximo turno procura.
    store = MemoryStore(max_note_bytes=700)
    for i in range(12):
        note = await store.write(
            user_id=USER, slug="n", content=f"conclusao numero {i} " + "x" * 80,
            now=datetime(2026, 7, 31, 12, i),
        )
    assert len(note.body.encode("utf-8")) <= 700
    assert "conclusao numero 11" in note.body       # a última sobrevive
    assert "conclusao numero 0" not in note.body    # a mais antiga caiu
    assert "removidas por limite" in note.body      # e o modelo sabe que caiu
    assert note.body.startswith("# ")               # cabeçalho preservado


@pytest.mark.asyncio
async def test_truncagem_de_entrada_unica_gigante_nao_estoura():
    store = MemoryStore(max_note_bytes=400)
    note = await store.write(user_id=USER, slug="n", content="y" * 5000)
    assert len(note.body.encode("utf-8")) <= 400


# ── índice ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_index_prioriza_nota_da_sessao_atual(store):
    await store.write(user_id=USER, slug="antiga", content="de outra sessão",
                      session_id=uuid4(), now=datetime(2026, 7, 31, 18, 0))
    await store.write(user_id=USER, slug="desta-sessao", content="tarefa em curso",
                      session_id=SESSION, now=datetime(2026, 7, 30, 9, 0))
    notes = await store.index(user_id=USER, session_id=SESSION)
    # A da sessão atual vem primeiro mesmo sendo MAIS ANTIGA.
    assert [n.slug for n in notes] == ["desta-sessao", "antiga"]


@pytest.mark.asyncio
async def test_index_ignora_arquivada_e_respeita_teto(store):
    for i in range(5):
        await store.write(user_id=USER, slug=f"n{i}", content="x",
                          now=datetime(2026, 7, 31, 10, i))
    await store.write(user_id=USER, slug="n0", content="acabou", status="done")
    notes = await store.index(user_id=USER, limit=3)
    assert len(notes) == 3
    assert "n0" not in [n.slug for n in notes]


@pytest.mark.asyncio
async def test_index_sem_usuario_e_vazio(store):
    assert await store.index(user_id=None) == []


# ── leitura ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_read_devolve_so_o_rabo_por_padrao(store):
    for i in range(8):
        await store.write(user_id=USER, slug="n", content=f"entrada {i}",
                          now=datetime(2026, 7, 31, 10, i))
    payload = await store.read(user_id=USER, slug="n")
    assert payload["entries"] == 8
    assert payload["omitted_entries"] == 4
    assert "entrada 7" in payload["body"] and "entrada 0" not in payload["body"]

    full = await store.read(user_id=USER, slug="n", full=True)
    assert "entrada 0" in full["body"]
    assert "omitted_entries" not in full


@pytest.mark.asyncio
async def test_read_de_nota_inexistente_orienta_em_vez_de_explodir(store):
    payload = await store.read(user_id=USER, slug="nao-existe")
    assert payload["error"] == "not_found"
    assert "wm_write" in payload["message"]


@pytest.mark.asyncio
async def test_read_aceita_slug_torto(store):
    await store.write(user_id=USER, slug="extrato-marco", content="x")
    assert (await store.read(user_id=USER, slug="Extrato Março"))["note"] == "extrato-marco"


# ── backstop automático ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_append_auto_usa_slug_da_sessao_e_nunca_levanta(store):
    note = await store.append_auto(user_id=USER, session_id=SESSION, entry="rodei um script")
    assert note is not None
    assert note.slug == f"sessao-{str(SESSION)[:8]}"
    assert note.origin == "auto"
    # Sem usuário / sem conteúdo: devolve None em vez de estourar no background.
    assert await store.append_auto(user_id=None, session_id=SESSION, entry="x") is None
    assert await store.append_auto(user_id=USER, session_id=SESSION, entry="  ") is None
