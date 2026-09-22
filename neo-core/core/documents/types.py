"""Tipos da leitura delegada (docs/deep-read-plan.md)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

#: Estados de um documento, na ordem em que acontecem.
DOC_STATUSES = ("pendente", "extraindo", "lendo", "resumindo", "feito", "falhou")


@dataclass
class ReadSpec:
    """O que o cérebro pediu. Vem do ``spec_json`` do job."""

    title: str
    goal: str
    questions: list[str]
    instructions: str = ""
    #: Cada item: ``{"path": "/workspace/..."}`` ou ``{"file_id": "..."}``.
    files: list[dict[str, str]] = field(default_factory=list)
    #: Pasta da leitura (absoluta, sob /workspace/analises).
    out_dir: str = ""
    job_id: str = ""
    #: Leitura ESTENDIDA (docs/deep-read-plan.md §10): o job de origem. Documentos
    #: já lidos lá são reaproveitados de ``.dados/<doc>.json``; só o que é novo
    #: (documento ou pergunta) é lido.
    extend_of: str = ""

    def numbered(self) -> list[tuple[int, str]]:
        return list(enumerate(self.questions, start=1))


@dataclass
class Finding:
    question: int
    page: int | None
    quote: str
    note: str
    part: int
    match: str = "exata"


@dataclass
class DocState:
    """Um documento da leitura — é isto que vira ``estado.json``."""

    index: int
    source: str
    name: str
    base: str
    status: str = "pendente"
    path: str = ""
    unit: str = "página"
    units: int = 0
    chars: int = 0
    parts: int = 0
    parts_read: int = 0
    parts_failed: int = 0
    method: str = ""
    ocr_pages: int = 0
    warnings: list[str] = field(default_factory=list)
    quotes_kept: int = 0
    quotes_dropped: int = 0
    error: str = ""
    summary: str = ""
    #: [{"pergunta": 1, "veredito": "...", "analise": "..."}]
    answers: list[dict[str, Any]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    #: Perguntas (texto) que este documento já respondeu — é o que a extensão
    #: compara para saber se precisa reler o documento.
    answered: list[str] = field(default_factory=list)
    #: Vem pronto de uma leitura anterior, sem reler nada.
    reused: bool = False

    @property
    def note_rel(self) -> str:
        return f"notas/{self.base}.md"

    @property
    def fully_read(self) -> bool:
        return self.parts > 0 and self.parts_read == self.parts

    def to_state(self) -> dict[str, Any]:
        """Tudo que a extensão precisa para não reler: ``.dados/<doc>.json``."""
        return {
            **self.to_json(),
            "base": self.base,
            "resumo": self.summary,
            "respostas": self.answers,
            "perguntas_respondidas": self.answered,
            "temas": self.topics,
            "achados": [
                {"q": f.question, "p": f.page, "citacao": f.quote, "nota": f.note, "parte": f.part, "match": f.match}
                for f in self.findings
            ],
        }

    def restore(self, state: dict[str, Any]) -> None:
        """Carrega o estado de uma leitura anterior do MESMO documento."""
        self.status = "feito"
        self.path = str(state.get("caminho") or self.path)
        self.name = str(state.get("nome") or self.name)
        self.unit = str(state.get("unidade") or self.unit)
        self.units = int(state.get("unidades") or 0)
        self.chars = int(state.get("chars") or 0)
        self.parts = int(state.get("partes") or 0)
        self.parts_read = int(state.get("partes_lidas") or 0)
        self.parts_failed = int(state.get("partes_falhas") or 0)
        self.method = str(state.get("metodo") or "")
        self.ocr_pages = int(state.get("ocr") or 0)
        self.warnings = [str(a) for a in state.get("avisos") or []]
        self.quotes_kept = int(state.get("citacoes") or 0)
        self.quotes_dropped = int(state.get("citacoes_descartadas") or 0)
        self.summary = str(state.get("resumo") or "")
        self.answers = [a for a in state.get("respostas") or [] if isinstance(a, dict)]
        self.answered = [str(q) for q in state.get("perguntas_respondidas") or []]
        self.topics = [str(t) for t in state.get("temas") or []]
        self.findings = [
            Finding(
                question=int(a.get("q") or 0), page=a.get("p"), quote=str(a.get("citacao") or ""),
                note=str(a.get("nota") or ""), part=int(a.get("parte") or 0), match=str(a.get("match") or "exata"),
            )
            for a in state.get("achados") or []
            if isinstance(a, dict)
        ]
        self.reused = True

    def to_json(self) -> dict[str, Any]:
        return {
            "nome": self.name,
            "origem": self.source,
            "caminho": self.path,
            "status": self.status,
            "unidade": self.unit,
            "unidades": self.units,
            "chars": self.chars,
            "partes": self.parts,
            "partes_lidas": self.parts_read,
            "partes_falhas": self.parts_failed,
            "metodo": self.method,
            "ocr": self.ocr_pages,
            "avisos": self.warnings,
            "citacoes": self.quotes_kept,
            "citacoes_descartadas": self.quotes_dropped,
            "erro": self.error,
            "nota": self.note_rel if self.status == "feito" else "",
        }


class ReaderSandbox(Protocol):
    async def run(self, command: str, *, cwd: str | None = None, timeout: int | None = None,
                  stall_timeout: int | None = None) -> Any: ...
    async def write_file(self, path: str, content: str) -> Any: ...
    async def read_file(self, path: str, *, max_bytes: int | None = None) -> Any: ...


class ReaderLLM(Protocol):
    async def complete(self, system: str, user: str, *, max_tokens: int) -> str: ...


class ReaderProgress(Protocol):
    async def update(self, docs: list[DocState], note: str, *, milestone: bool) -> None:
        """``milestone`` = um documento mudou de fase (vale gravar o caderno);
        sem ele é só a linha de progresso de uma parte lida."""
        ...
