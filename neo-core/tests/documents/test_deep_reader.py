"""DeepReader de ponta a ponta com o extrator REAL rodando num sandbox em disco.

O sandbox falso troca ``/workspace`` por uma pasta temporária e executa os
comandos de verdade — então o script de extração e o fatiamento por página são
exercitados como no container. Só o LLM é falso: ele "lê" o trecho, devolve
uma citação verdadeira e uma inventada, e o teste confere que só a verdadeira
chegou às notas.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from core.documents import DeepReader, ReadSpec, doc_base_names, render_result_summary
from core.documents.render import render_memory_note


@dataclass
class _Res:
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class _DiskSandbox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.commands: list[str] = []

    def _real(self, text: str) -> str:
        return text.replace("/workspace", str(self.root))

    async def run(self, command, *, cwd=None, timeout=None, stall_timeout=None):
        self.commands.append(command)
        cmd = self._real(command).replace("python3 ", f"{sys.executable} ", 1)
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        return _Res(proc.returncode, out.decode(), err.decode())

    async def write_file(self, path, content):
        real = Path(self._real(path))
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text(content, encoding="utf-8")
        return _Res(0)

    async def read_file(self, path, *, max_bytes=None):
        real = Path(self._real(path))
        if not real.is_file():
            return _Res(1, "", "No such file")
        return _Res(0, real.read_text(encoding="utf-8"))


class _ReaderLLM:
    """Cita a primeira frase de verdade do trecho — e inventa uma."""

    def __init__(self) -> None:
        self.map_calls = 0
        self.reduce_calls = 0

    async def complete(self, system, user, *, max_tokens):
        if "<<<TRECHO" in user:
            self.map_calls += 1
            trecho = user.split("<<<TRECHO\n", 1)[1].split("\nTRECHO>>>", 1)[0]
            frases = [
                ln.strip() for ln in trecho.splitlines()
                if ln.strip() and not ln.startswith("[[") and "imposto" in ln
            ]
            achados = [
                {"pergunta": 2, "pagina": 99, "citacao": "a empresa promete dobrar o faturamento em um mês", "nota": "inventada"},
            ]
            if frases:
                achados.append({"pergunta": 1, "pagina": 1, "citacao": frases[0], "nota": "fala de imposto"})
            return json.dumps({"achados": achados, "temas": "impostos"})
        self.reduce_calls += 1
        return json.dumps({
            "resumo": "Documento sobre impostos.",
            "perguntas": [
                {"pergunta": 1, "veredito": "Sim — cria imposto (p. 2)", "analise": "Propõe imposto novo (p. 2)."},
                {"pergunta": 2, "veredito": "Não encontrado", "analise": "Nada sobre regulação no documento lido por inteiro."},
            ],
        })


class _Progress:
    def __init__(self) -> None:
        self.notes: list[tuple[str, bool]] = []

    async def update(self, docs, note, *, milestone):
        self.notes.append((note, milestone))


def _spec(root: Path, files: list[dict]) -> ReadSpec:
    return ReadSpec(
        title="Planos teste",
        goal="comparar planos",
        questions=["aumento de carga tributária", "regulação"],
        files=files,
        out_dir="/workspace/analises/planos-teste-abcd1234",
        job_id="abcd1234-0000-0000-0000-000000000000",
    )


def _paginas(n: int, marca: str) -> str:
    # Mesmo formato do pdftotext: páginas separadas por form feed.
    return "\f".join(
        f"Página {i} do plano {marca}.\nVamos criar um imposto sobre {marca} número {i} para financiar.\n"
        + ("Texto de enchimento sem relevância nenhuma. " * 40)
        for i in range(1, n + 1)
    )


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "files").mkdir()
    return tmp_path


def _txt_as_pages(workspace: Path, name: str, text: str) -> str:
    # O extrator lê .txt como uma seção só; para exercitar páginas usamos um
    # "pdf" falso: o script chama `pdftotext`, que substituímos por um cat.
    (workspace / "files" / name).write_text(text, encoding="utf-8")
    return f"/workspace/files/{name}"


@pytest.fixture
def fake_pdftotext(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "pdftotext"
    script.write_text('#!/bin/sh\ncat "$3"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{Path(shutil.which('sh')).parent}:/usr/bin:/bin")
    return bindir


@pytest.mark.asyncio
async def test_le_dois_documentos_aterra_citacoes_e_escreve_notas_e_sintese(workspace, fake_pdftotext):
    a = _txt_as_pages(workspace, "Plano_A.pdf", _paginas(9, "herança"))
    b = _txt_as_pages(workspace, "Plano_B.pdf", _paginas(3, "lucros"))
    llm, progress = _ReaderLLM(), _Progress()
    sandbox = _DiskSandbox(workspace)
    reader = DeepReader(
        spec=_spec(workspace, [{"path": a}, {"path": b}]),
        sandbox=sandbox, llm=llm, progress=progress, chunk_chars=4000,
        clock=lambda: datetime(2026, 9, 16, 20, 0),
    )
    report = await reader.run()

    assert report.status == "completed"
    assert [d.status for d in report.docs] == ["feito", "feito"]
    doc_a = report.docs[0]
    assert doc_a.units == 9 and doc_a.parts > 1 and doc_a.fully_read
    # A citação inventada nunca vira evidência; a verdadeira, sim — com a página
    # REAL (o modelo disse 1 para todas).
    assert doc_a.quotes_dropped == doc_a.parts
    assert doc_a.quotes_kept >= 1
    assert all("dobrar o faturamento" not in f.quote for f in doc_a.findings)
    assert {f.page for f in doc_a.findings} - {1} != set()

    base = workspace / "analises" / "planos-teste-abcd1234"
    nota = (base / "notas" / "Plano_A.md").read_text()
    assert "## 1. aumento de carga tributária" in nota
    assert "Vamos criar um imposto sobre herança" in nota
    assert "dobrar o faturamento em um mês" not in nota
    assert "TOTAL — 9 páginas" in nota

    sintese = (base / "sintese.md").read_text()
    assert "## Quadro comparativo" in sintese
    assert "| Plano_A.pdf | Sim — cria imposto (p. 2) | Não encontrado |" in sintese
    assert "notas/Plano_B.md" in sintese
    # Sem citações na síntese: elas moram nas notas.
    assert "Vamos criar um imposto" not in sintese

    estado = json.loads((base / "estado.json").read_text())
    assert [d["status"] for d in estado["documentos"]] == ["feito", "feito"]
    # Cópia integral do texto não sobra no servidor.
    assert not (base / ".texto").exists()
    assert any(m for _, m in progress.notes)


@pytest.mark.asyncio
async def test_documento_que_falha_nao_derruba_os_outros(workspace, fake_pdftotext):
    bom = _txt_as_pages(workspace, "Bom.pdf", _paginas(2, "renda"))
    reader = DeepReader(
        spec=_spec(workspace, [{"path": "/workspace/files/Nao_Existe.pdf"}, {"path": bom}]),
        sandbox=_DiskSandbox(workspace), llm=_ReaderLLM(), progress=_Progress(), chunk_chars=4000,
    )
    report = await reader.run()
    assert report.status == "completed"
    assert report.docs[0].status == "falhou" and report.docs[0].error
    assert report.docs[1].status == "feito"

    resumo = render_result_summary(reader.spec, report.docs, synthesis_bytes=report.synthesis_bytes)
    assert "1/2 documento(s)" in resumo
    assert "✗ Nao_Existe.pdf" in resumo
    assert "sintese.md" in resumo
    # O resumo é ponteiro: nada do conteúdo lido.
    assert "imposto sobre renda" not in resumo


@pytest.mark.asyncio
async def test_parte_que_o_llm_nao_consegue_ler_deixa_a_cobertura_parcial(workspace, fake_pdftotext):
    doc = _txt_as_pages(workspace, "Longo.pdf", _paginas(6, "consumo"))

    class _Flaky(_ReaderLLM):
        async def complete(self, system, user, *, max_tokens):
            if "parte 2 de" in user:
                raise RuntimeError("provider 500")
            return await super().complete(system, user, max_tokens=max_tokens)

    reader = DeepReader(
        spec=_spec(workspace, [{"path": doc}]), sandbox=_DiskSandbox(workspace),
        llm=_Flaky(), progress=_Progress(), chunk_chars=4000,
    )
    report = await reader.run()
    d = report.docs[0]
    assert d.status == "feito" and not d.fully_read and d.parts_failed == 1
    nota = (workspace / "analises" / "planos-teste-abcd1234" / "notas" / "Longo.md").read_text()
    assert "PARCIAL" in nota


@pytest.mark.asyncio
async def test_file_id_e_materializado_pelo_stager(workspace, fake_pdftotext):
    _txt_as_pages(workspace, "Catalogo.pdf", _paginas(2, "herança"))

    async def stager(file_id):
        assert file_id == "83b9416d"
        return "/workspace/files/Catalogo.pdf", "Catalogo.pdf"

    reader = DeepReader(
        spec=_spec(workspace, [{"file_id": "83b9416d"}]), sandbox=_DiskSandbox(workspace),
        llm=_ReaderLLM(), progress=_Progress(), stager=stager, chunk_chars=4000,
    )
    report = await reader.run()
    assert report.docs[0].status == "feito"
    assert report.docs[0].name == "Catalogo.pdf"


def test_nomes_base_sao_seguros_e_unicos():
    assert doc_base_names(["/workspace/files/Relatorio Charlie.pdf", "/x/Relatorio_Charlie.pdf", "/x/.pdf"]) == [
        "Relatorio_Charlie", "Relatorio_Charlie-2", "documento-3",
    ]


def test_nota_do_caderno_so_tem_endereco_e_status():
    from core.documents.types import DocState

    spec = ReadSpec(
        title="Contratos", goal="achar multas", questions=["multa rescisória"],
        out_dir="/workspace/analises/contratos-1a2b3c4d", job_id="1a2b3c4d-x",
    )
    docs = [
        DocState(index=0, source="p", name="c1.pdf", base="c1", status="feito", summary="SEGREDO DO CONTRATO"),
        DocState(index=1, source="p", name="c2.pdf", base="c2", status="lendo"),
        DocState(index=2, source="p", name="c3.pdf", base="c3"),
    ]
    corpo, linha = render_memory_note(spec, docs, datetime(2026, 9, 16, 20, 0))
    assert "/workspace/analises/contratos-1a2b3c4d/" in corpo
    assert "1/3 feitos (c1.pdf)" in corpo and "em andamento: c2.pdf" in corpo and "pendentes: c3.pdf" in corpo
    assert "SEGREDO" not in corpo
    assert len(corpo) < 800 and len(linha) <= 240
    assert re.search(r"1/3 documentos", linha)
