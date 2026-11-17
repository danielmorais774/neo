"""Correção incremental de uma leitura (docs/deep-read-plan.md §10).

Sessão ad3be3bf: "eu não vi falar do Foxtrot". O Neo abriu uma leitura NOVA só do
Foxtrot, com outras perguntas, e reescreveu a conclusão do zero a partir de duas
sínteses que não conversavam — perdeu o que já tinha apurado dos outros e deixou
"Sem menção específica" onde antes havia conclusão. Estender é acrescentar: o
que já foi lido volta de ``.dados/``, e só o novo é lido.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from core.documents import DeepReader, ReadSpec
from core.documents.render import SYNTHESIS_MAX_BYTES, render_synthesis
from core.documents.types import DocState
from tests.documents.test_deep_reader import (  # noqa: F401
    _DiskSandbox,
    _paginas,
    _Progress,
    _ReaderLLM,
    _txt_as_pages,
    fake_pdftotext,
)

OUT = "/workspace/analises/planos-ext-11112222"


class _SpyLLM(_ReaderLLM):
    def __init__(self):
        super().__init__()
        self.map_users: list[str] = []

    async def complete(self, system, user, *, max_tokens):
        if "<<<TRECHO" in user:
            self.map_users.append(user)
            trecho = user.split("<<<TRECHO\n", 1)[1].split("\nTRECHO>>>", 1)[0]
            frase = next(ln.strip() for ln in trecho.splitlines() if "imposto" in ln)
            numeros = [int(n) for n in __import__("re").findall(r"^(\d+)\. ", user.split("Documento:")[0], __import__("re").M)]
            return json.dumps({
                "achados": [{"pergunta": n, "pagina": 1, "citacao": frase, "nota": f"q{n}"} for n in numeros],
                "temas": "impostos",
            })
        self.reduce_calls += 1
        numeros = [int(n) for n in __import__("re").findall(r"^## Pergunta (\d+):", user, __import__("re").M)]
        return json.dumps({
            "resumo": f"resumo com {len(numeros)} perguntas",
            "perguntas": [{"pergunta": n, "veredito": f"veredito {n}", "analise": f"analise {n}"} for n in numeros],
        })


def _spec(files, questions, *, extend=False):
    return ReadSpec(
        title="Planos", goal="comparar", questions=questions, files=[{"path": f} for f in files],
        out_dir=OUT, job_id="11112222-aaaa", extend_of="00000000-base" if extend else "",
    )


async def _ler(ws, files, questions, *, extend=False):
    llm = _SpyLLM()
    reader = DeepReader(
        spec=_spec(files, questions, extend=extend), sandbox=_DiskSandbox(ws), llm=llm,
        progress=_Progress(), chunk_chars=4000,
    )
    return await reader.run(), llm


@pytest.mark.asyncio
async def test_acrescentar_documento_nao_rele_os_que_ja_foram_lidos(tmp_path, fake_pdftotext):
    (tmp_path / "files").mkdir()
    a = _txt_as_pages(tmp_path, "Charlie.pdf", _paginas(3, "renda"))
    b = _txt_as_pages(tmp_path, "Echo.pdf", _paginas(3, "consumo"))
    c = _txt_as_pages(tmp_path, "Foxtrot.pdf", _paginas(3, "herança"))
    perguntas = ["carga tributária", "regulação"]

    primeira, _ = await _ler(tmp_path, [a, b], perguntas)
    assert [d.status for d in primeira.docs] == ["feito", "feito"]

    segunda, llm = await _ler(tmp_path, [a, b, c], perguntas, extend=True)

    assert [d.status for d in segunda.docs] == ["feito", "feito", "feito"]
    assert [d.reused for d in segunda.docs] == [True, True, False]
    # Só o Foxtrot foi lido: nenhuma parte do Charlie ou do Echo voltou ao LLM.
    assert llm.map_users and all("Documento: Foxtrot.pdf" in u for u in llm.map_users)
    assert llm.reduce_calls == 1
    # O que já estava apurado continua na síntese — nada virou vazio.
    sintese = (tmp_path / "analises" / "planos-ext-11112222" / "sintese.md").read_text()
    for nome in ("Charlie.pdf", "Echo.pdf", "Foxtrot.pdf"):
        assert f"| {nome} | veredito 1 | veredito 2 |" in sintese


@pytest.mark.asyncio
async def test_pergunta_nova_rele_so_para_ela_e_mantem_as_respostas_antigas(tmp_path, fake_pdftotext):
    (tmp_path / "files").mkdir()
    a = _txt_as_pages(tmp_path, "Charlie.pdf", _paginas(3, "renda"))

    await _ler(tmp_path, [a], ["carga tributária", "regulação"])
    nota_antes = (tmp_path / "analises" / "planos-ext-11112222" / "notas" / "Charlie.md").read_text()
    assert "## 2. regulação" in nota_antes

    relatorio, llm = await _ler(tmp_path, [a], ["carga tributária", "regulação", "metas"], extend=True)

    d = relatorio.docs[0]
    # O leitor só recebeu a pergunta nova, com o número GLOBAL dela.
    assert llm.map_users and all("3. metas" in u and "1. carga" not in u for u in llm.map_users)
    assert sorted(a["pergunta"] for a in d.answers) == [1, 2, 3]
    assert {a["veredito"] for a in d.answers} == {"veredito 1", "veredito 2", "veredito 3"}
    assert {f.question for f in d.findings} == {1, 2, 3}
    # O resumo antigo fica: o novo só viu a pergunta acrescentada.
    assert d.summary == "resumo com 2 perguntas"
    nota = (tmp_path / "analises" / "planos-ext-11112222" / "notas" / "Charlie.md").read_text()
    assert "## 1. carga tributária" in nota and "## 3. metas" in nota


@pytest.mark.asyncio
async def test_extensao_sem_estado_anterior_le_de_novo_em_vez_de_perder(tmp_path, fake_pdftotext):
    (tmp_path / "files").mkdir()
    a = _txt_as_pages(tmp_path, "Antigo.pdf", _paginas(2, "renda"))
    relatorio, llm = await _ler(tmp_path, [a], ["carga tributária"], extend=True)
    assert relatorio.docs[0].status == "feito" and not relatorio.docs[0].reused
    assert llm.map_users


def test_sintese_sempre_cabe_numa_leitura_e_mostra_todos_os_documentos():
    """Sessão ad3be3bf: 42,8 KB, corte em 32 KB, Echo fora da conclusão."""
    perguntas = [f"eixo número {i} com uma descrição comprida do que procurar" for i in range(1, 10)]
    spec = ReadSpec(title="Planos", goal="comparar", questions=perguntas, out_dir=OUT)
    docs = []
    for i, nome in enumerate(["Alfa", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]):
        d = DocState(index=i, source=nome, name=f"{nome}.pdf", base=nome, status="feito", parts=10, parts_read=10, units=80)
        d.summary = "Resumo longo do plano. " * 60
        d.answers = [
            {"pergunta": n, "veredito": f"Sim — proposta {n} de {nome}", "analise": "Análise detalhada com páginas (p. 12). " * 60}
            for n in range(1, 10)
        ]
        docs.append(d)

    texto = render_synthesis(spec, docs, datetime(2026, 9, 17, 1, 0))

    assert len(texto.encode("utf-8")) <= SYNTHESIS_MAX_BYTES
    for nome in ("Alfa", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"):
        assert f"| {nome}.pdf |" in texto and f"### {nome}.pdf" in texto
    assert "Compactada para caber numa leitura" in texto
    assert "continua em notas/" in texto or "Sim — proposta 9 de Foxtrot" in texto


def test_sintese_pequena_nao_e_compactada():
    spec = ReadSpec(title="t", goal="g", questions=["q"], out_dir=OUT)
    d = DocState(index=0, source="a", name="a.pdf", base="a", status="feito", parts=1, parts_read=1, units=1)
    d.summary = "curto"
    d.answers = [{"pergunta": 1, "veredito": "sim", "analise": "análise curta"}]
    texto = render_synthesis(spec, [d], datetime(2026, 9, 17))
    assert "Compactada" not in texto and "análise curta" in texto
