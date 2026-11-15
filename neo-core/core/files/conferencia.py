"""O que a resposta AFIRMA que está no arquivo está mesmo no arquivo?

Determinístico de propósito, igual à guarda anti-alegação de artefato: roda no fim
do turno que publicou um documento e não pode custar uma chamada de LLM.

session-20cb02b6, turno 4: a verificação do próprio Neo imprimiu a linha da tabela
com a célula do R.A. VAZIA, e a resposta disse "R.A. **12345678-9**". Escopo
honesto desta guarda: ela confere se cada VALOR destacado na resposta (em
**negrito** ou `código`) aparece em ALGUM lugar do arquivo salvo. Pega valor
inventado ou trocado (um dígito, o nome sem o "Carlos"), versão errada salva. Não
sabe EM QUE parte do documento o valor está — naquele turno o R.A. estava no
cabeçalho de página do Word e faltava na tabela, e isso é trabalho do
`docx_conferir.py` + `file_olhar` da skill docx.
"""

from __future__ import annotations

import re
import unicodedata
import zipfile
from pathlib import Path

#: Partes de texto de cada formato zipado (o que um leitor humano vê).
_PARTES_ZIP = {
    "docx": re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$"),
    "pptx": re.compile(r"^ppt/slides/slide\d+\.xml$"),
    "xlsx": re.compile(r"^xl/(sharedStrings|worksheets/sheet\d+)\.xml$"),
    "odt": re.compile(r"^content\.xml$"),
}
_TEXTO_PURO = {"txt", "md", "csv", "tsv", "html", "htm", "json", "xml", "yaml", "yml", "tex", "rtf"}
_TAG_TEXTO = re.compile(r"<(?:w:t|a:t|t|text:[a-z-]+)(?:\s[^>]*)?>([^<]*)<", re.IGNORECASE)
_FIM_DE_BLOCO = re.compile(r"</(?:w:p|a:p|text:p|si|row)>", re.IGNORECASE)
_MAX_BYTES = 30 * 1024 * 1024

_DESTAQUE = re.compile(r"\*\*([^*\n]{2,120})\*\*|`([^`\n]{2,120})`")
_NOME_PROPRIO = re.compile(r"^[A-ZÀ-Ý][a-zà-ÿ]+(?:\s+(?:d[aeo]s?\s+)?[A-ZÀ-Ý][a-zà-ÿ]+)+$")
#: Nome de arquivo ou caminho — não data: "relatorio.md", "/workspace/x", "~/Desktop"
#: saem; "29/11/2026" e "v1.2" ficam.
_ARQUIVO = re.compile(r"\.[A-Za-z][A-Za-z0-9]{1,4}$|(?:^|\s)[/~]|/[A-Za-z_]")


def _plano(texto: str) -> str:
    normal = unicodedata.normalize("NFKD", texto or "")
    sem = "".join(c for c in normal if not unicodedata.combining(c))
    return " ".join(sem.lower().split())


def valores_afirmados(resposta: str) -> list[str]:
    """Valores concretos que a resposta destaca: identificador com número ou nome próprio.

    "R.A. **12345678-9**", "**Carlos Daniel Morais**", "**29/11/2026 até 23:59**"
    entram; "**Cabeçalho**", "**As 10 questões**", "`questoes_preenchido.docx`",
    "`[ X ]`" não — palavra solta, contagem e nome de arquivo não são o conteúdo.
    """
    out: list[str] = []
    for m in _DESTAQUE.finditer(resposta or ""):
        v = (m.group(1) or m.group(2) or "").strip().strip(".,;:")
        if not v or _ARQUIVO.search(v):
            continue
        digitos = sum(c.isdigit() for c in v)
        identificador = digitos >= 5 or (digitos >= 3 and re.search(r"\d[-/.:]\d", v) is not None)
        if (identificador or _NOME_PROPRIO.match(v)) and v not in out:
            out.append(v)
    return out


def texto_do_arquivo(caminho: Path, nome: str) -> str | None:
    """Texto legível do arquivo, ou None quando o formato não é conferível aqui."""
    ext = (nome or caminho.name).rsplit(".", 1)[-1].lower() if "." in (nome or caminho.name) else ""
    try:
        if caminho.stat().st_size > _MAX_BYTES:
            return None
        if ext in _TEXTO_PURO:
            return caminho.read_text(encoding="utf-8", errors="replace")
        padrao = _PARTES_ZIP.get(ext)
        if padrao is None:
            return None
        pedacos: list[str] = []
        with zipfile.ZipFile(caminho) as z:
            for parte in z.namelist():
                if padrao.match(parte):
                    xml = z.read(parte).decode("utf-8", errors="replace")
                    xml = _FIM_DE_BLOCO.sub("\n<", xml)
                    pedacos.append("".join(_TAG_TEXTO.findall(xml)) + "\n")
        return "".join(pedacos)
    except (OSError, zipfile.BadZipFile, KeyError):
        return None


def ausentes(valores: list[str], texto: str) -> list[str]:
    """Os valores que NÃO aparecem no texto (sem acento, sem caixa, espaço colapsado)."""
    plano = _plano(texto)
    return [v for v in valores if _plano(v) not in plano]
