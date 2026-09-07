"""Registro de artefatos — a nota rolante que guarda ONDE o arquivo ficou.

O caso que originou tudo (sessão 081cd1c3): o Neo baixou `nfse_marco.pdf` pelo
navegador local, disse "está na sua pasta de downloads" em PROSA, e no turno
seguinte o classificador teve que remontar o caminho de cabeça. O `dir` real
vinha do device em toda resposta e era descartado.

O que estes testes travam:

- a linha do registro é redonda: o que é escrito é lido de volta como dado;
- o caminho inteiro cabe no ``summary``, que é o que entra no prompt TODO turno
  — é isso que torna o ``wm_read`` desnecessário para "onde está o arquivo";
- o download registrado quando COMEÇA (nome temporário, tamanho parcial) e a
  confirmação do fim colapsam em UMA linha, não duas.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from core.working_memory import artifacts

USER = uuid4()
SESSION = uuid4()


# ── formato: escrever e ler de volta ──────────────────────────────────────


def test_linha_ida_e_volta() -> None:
    item = {
        "name": "nfse_marco.pdf",
        "path": "/Users/daniel/Downloads/nfse_marco.pdf",
        "bytes": 72643,
        "source": "baixado pelo navegador local",
    }
    body = artifacts.render_entry([item])
    lido = artifacts.parse_artifacts(body)

    assert lido == [
        {
            "name": "nfse_marco.pdf",
            "path": "/Users/daniel/Downloads/nfse_marco.pdf",
            "bytes": 72643,
            "source": "baixado pelo navegador local",
        }
    ]


def test_item_sem_caminho_nao_vira_linha() -> None:
    """Nome sem caminho é exatamente o que já existia (prosa) — não serve."""
    assert artifacts.render_line({"name": "x.pdf", "path": ""}) == ""
    assert artifacts.render_entry([{"name": "x.pdf"}]) == ""


def test_prosa_ao_redor_nao_polui_a_leitura() -> None:
    body = (
        "# Arquivos que eu trouxe\n"
        "> criado 30/08 03:00\n\n"
        "## 30/08 03:00\n"
        "Baixei estes arquivos na máquina do usuário.\n"
        "⬇ nfse_marco.pdf · /Users/daniel/Downloads/nfse_marco.pdf · 72643 bytes\n"
        "- isto é um bullet comum e não é artefato\n"
    )
    lido = artifacts.parse_artifacts(body)
    assert [i["name"] for i in lido] == ["nfse_marco.pdf"]


# ── o índice carrega a resposta, não um ponteiro ──────────────────────────


def test_summary_carrega_o_caminho_inteiro() -> None:
    """É a linha que o modelo vê todo turno sem gastar tool call."""
    resumo = artifacts.render_summary(
        [{"name": "nfse_marco.pdf", "path": "/Users/daniel/Downloads/nfse_marco.pdf"}]
    )
    assert "/Users/daniel/Downloads/nfse_marco.pdf" in resumo


def test_summary_cabe_no_campo_do_indice() -> None:
    """240 chars é o teto de `working_note.summary` — estourar trunca o caminho."""
    from core.working_memory.store import MAX_SUMMARY_LEN

    resumo = artifacts.render_summary(
        [
            {"name": f"arquivo_{i}.pdf", "path": f"/Users/daniel/Downloads/arquivo_{i}.pdf"}
            for i in range(2)
        ]
    )
    assert len(resumo) <= MAX_SUMMARY_LEN


# ── começo do download × confirmação do fim ───────────────────────────────


def test_nome_temporario_vira_nome_final() -> None:
    """`nfse.pdf.crdownload` e `nfse.pdf` são o MESMO arquivo, não dois."""
    item = artifacts.normalize(
        {
            "name": "nfse_marco.pdf.crdownload",
            "path": "/Users/daniel/Downloads/nfse_marco.pdf.crdownload",
            "incomplete": True,
        }
    )
    assert item["name"] == "nfse_marco.pdf"
    assert item["path"] == "/Users/daniel/Downloads/nfse_marco.pdf"


def test_tamanho_parcial_nao_e_registrado() -> None:
    """Byte de download em andamento mente — registrar viraria número errado."""
    item = artifacts.normalize(
        {"name": "a.zip", "path": "/d/a.zip", "bytes": 12, "incomplete": True}
    )
    assert item["bytes"] is None
    assert "ainda baixando" in (item["source"] or "")


def test_comeco_e_confirmacao_colapsam_numa_linha() -> None:
    """Duas gravações, um arquivo: a última (com tamanho) é a que vale."""
    comeco = artifacts.render_entry(
        [artifacts.normalize({
            "name": "nfse_marco.pdf.crdownload",
            "path": "/Users/daniel/Downloads/nfse_marco.pdf.crdownload",
            "incomplete": True,
            "source": "baixado pelo navegador local",
        })]
    )
    fim = artifacts.render_entry(
        [artifacts.normalize({
            "name": "nfse_marco.pdf",
            "path": "/Users/daniel/Downloads/nfse_marco.pdf",
            "bytes": 72643,
            "source": "baixado pelo navegador local",
        })]
    )

    lido = artifacts.parse_artifacts(comeco + "\n\n" + fim)

    assert len(lido) == 1
    assert lido[0]["bytes"] == 72643
    assert "ainda baixando" not in (lido[0]["source"] or "")


# ── as duas superfícies de leitura ────────────────────────────────────────


def test_path_map_mais_recente_vence() -> None:
    itens = [
        {"name": "nota.pdf", "path": "/Users/daniel/Downloads/nota.pdf"},
        {"name": "nota.pdf", "path": "/velho/nota.pdf"},
    ]
    assert artifacts.path_map(itens)["nota.pdf"] == "/Users/daniel/Downloads/nota.pdf"


def test_bloco_de_contexto_manda_usar_o_caminho_exato() -> None:
    bloco = artifacts.render_context_block(
        [{"name": "nfse_marco.pdf", "path": "/Users/daniel/Downloads/nfse_marco.pdf"}]
    )
    assert "/Users/daniel/Downloads/nfse_marco.pdf" in bloco
    assert "não monte caminho a partir do nome" in bloco


def test_bloco_vazio_sem_artefato() -> None:
    """Sem artefato, nem uma linha entra no prompt do classificador."""
    assert artifacts.render_context_block([]) == ""


# ── gravação e leitura contra um store de mentira ─────────────────────────


class _FakeStore:
    """Só o contrato que o registro usa: ``write`` e ``read``."""

    def __init__(self) -> None:
        self.body = ""
        self.summary = ""
        self.writes = 0

    async def write(self, *, content: str, summary: str | None = None, **_: object):
        self.writes += 1
        self.body = (self.body + "\n\n" + content).strip()
        self.summary = summary or ""
        return object()

    async def read(self, *, user_id, slug, full=False):  # noqa: ARG002
        return {"body": self.body}


@pytest.mark.asyncio
async def test_record_e_recent_do_mais_novo_para_o_mais_velho() -> None:
    store = _FakeStore()
    await artifacts.record(
        store, user_id=USER, session_id=SESSION,
        items=[{"name": "a.pdf", "path": "/d/a.pdf", "bytes": 10}],
    )
    await artifacts.record(
        store, user_id=USER, session_id=SESSION,
        items=[{"name": "b.pdf", "path": "/d/b.pdf", "bytes": 20}],
    )

    itens = await artifacts.recent(store, user_id=USER)

    assert [i["name"] for i in itens] == ["b.pdf", "a.pdf"]
    assert "/d/b.pdf" in store.summary


@pytest.mark.asyncio
async def test_record_nunca_levanta() -> None:
    """É chamado de job em segundo plano: falhar aqui não pode custar o job."""

    class _Explode:
        async def write(self, **_: object):
            raise RuntimeError("banco fora")

    assert await artifacts.record(
        _Explode(), user_id=USER, session_id=SESSION,
        items=[{"name": "a.pdf", "path": "/d/a.pdf"}],
    ) is None


@pytest.mark.asyncio
async def test_recent_sem_nota_devolve_vazio() -> None:
    class _Vazio:
        async def read(self, **_: object):
            return {"error": "not_found", "message": "não existe"}

    assert await artifacts.recent(_Vazio(), user_id=USER) == []
