"""Extração + fatiamento de um documento DENTRO do sandbox (docs/deep-read-plan.md §4.1).

O script vive aqui como texto e é gravado no /workspace a cada job: a imagem do
workbench já tem pdftotext/pytesseract/python-docx/python-pptx/pandas/html2text
(neo-workbench-document-toolset), e rodar lá dentro mantém o documento fora do
processo do neo-core — só volta um JSON curto com os números.

Saída em ``<out_dir>``:
  partes.json          [{"n", "de", "ate", "chars", "arquivo"}]
  parte-001.txt …      texto com marcadores ``[[página N]]`` (ou slide/planilha)
e uma linha JSON no stdout: ``{"ok", "unidade", "unidades", "chars", "partes",
"metodo", "ocr", "avisos"}``.
"""

from __future__ import annotations

import shlex

#: Onde o script é gravado no sandbox. Fora de `analises/` para não parecer produto.
SCRIPT_PATH = "/workspace/.neo/deep_read_extract.py"

EXTRACT_SCRIPT = r'''
import json, os, re, subprocess, sys

src, outdir = sys.argv[1], sys.argv[2]
chunk_chars, max_ocr = int(sys.argv[3]), int(sys.argv[4])
ext = os.path.splitext(src)[1].lower()
avisos = []
ocr = 0

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u00ad]")

def limpa(t):
    t = (t or "").replace("\r\n", "\n").replace("\r", "\n")
    t = _CTRL.sub("", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()

def pdf():
    global ocr
    r = subprocess.run(["pdftotext", "-enc", "UTF-8", src, "-"], capture_output=True)
    if r.returncode != 0:
        avisos.append("pdftotext: " + r.stderr.decode("utf-8", "replace")[:200])
    paginas = r.stdout.decode("utf-8", "replace").split("\f")
    if paginas and not paginas[-1].strip():
        paginas.pop()
    vazias = [i for i, p in enumerate(paginas) if len(p.strip()) < 40]
    metodo = "pdftotext"
    # Escaneado: a maioria das páginas sem texto. OCR é lento (segundos por
    # página), então tem teto e o aviso diz o que ficou de fora.
    if paginas and len(vazias) > len(paginas) / 2:
        try:
            import pytesseract
            from pdf2image import convert_from_path
            alvo = vazias[:max_ocr]
            for i in alvo:
                imgs = convert_from_path(src, dpi=200, first_page=i + 1, last_page=i + 1)
                if imgs:
                    paginas[i] = pytesseract.image_to_string(imgs[0], lang="por")
                    ocr += 1
            metodo = "pdftotext+ocr"
            if len(vazias) > len(alvo):
                avisos.append(f"OCR limitado a {len(alvo)} de {len(vazias)} páginas sem texto")
        except Exception as e:
            avisos.append(f"OCR indisponível: {e}")
    return paginas, "página", metodo

def docx():
    import docx as _d
    d = _d.Document(src)
    blocos = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            blocos.append(" | ".join(c.text for c in row.cells))
    return ["\n".join(blocos)], "seção", "python-docx"

def pptx():
    from pptx import Presentation
    slides = []
    for s in Presentation(src).slides:
        textos = [sh.text_frame.text for sh in s.shapes if getattr(sh, "has_text_frame", False)]
        slides.append("\n".join(textos))
    return slides, "slide", "python-pptx"

def planilha():
    import pandas as pd
    if ext == ".csv":
        return [pd.read_csv(src, sep=None, engine="python").to_csv(index=False)], "planilha", "pandas"
    folhas = pd.read_excel(src, sheet_name=None)
    return [f"# {nome}\n" + df.to_csv(index=False) for nome, df in folhas.items()], "planilha", "pandas"

def html():
    import html2text
    with open(src, encoding="utf-8", errors="replace") as f:
        return [html2text.html2text(f.read())], "seção", "html2text"

def texto():
    with open(src, encoding="utf-8", errors="replace") as f:
        return [f.read()], "seção", "texto"

leitores = {".pdf": pdf, ".docx": docx, ".pptx": pptx, ".xlsx": planilha, ".xls": planilha,
            ".ods": planilha, ".csv": planilha, ".html": html, ".htm": html}
unidades, unidade, metodo = leitores.get(ext, texto)()
unidades = [limpa(u) for u in unidades]

# Unidade maior que a parte (documento sem páginas, página enorme): quebra por
# parágrafo mantendo o número da unidade — a citação continua localizável.
pedacos = []
for i, u in enumerate(unidades, start=1):
    if len(u) <= chunk_chars:
        pedacos.append((i, u))
        continue
    atual = ""
    for par in u.split("\n\n"):
        while len(par) > chunk_chars:
            if atual:
                pedacos.append((i, atual)); atual = ""
            pedacos.append((i, par[:chunk_chars])); par = par[chunk_chars:]
        if atual and len(atual) + len(par) + 2 > chunk_chars:
            pedacos.append((i, atual)); atual = ""
        atual = f"{atual}\n\n{par}" if atual else par
    if atual:
        pedacos.append((i, atual))

os.makedirs(outdir, exist_ok=True)
partes, buf, de, ate = [], [], None, None
def fecha():
    global buf, de, ate
    if not buf:
        return
    n = len(partes) + 1
    nome = f"parte-{n:03d}.txt"
    corpo = "\n\n".join(buf)
    with open(os.path.join(outdir, nome), "w", encoding="utf-8") as f:
        f.write(corpo)
    partes.append({"n": n, "de": de, "ate": ate, "chars": len(corpo), "arquivo": nome})
    buf, de, ate = [], None, None

tamanho = 0
for num, txt in pedacos:
    if not txt.strip():
        continue
    bloco = f"[[{unidade} {num}]]\n{txt}"
    if buf and tamanho + len(bloco) > chunk_chars:
        fecha(); tamanho = 0
    buf.append(bloco); tamanho += len(bloco) + 2
    de = num if de is None else de
    ate = num
fecha()

with open(os.path.join(outdir, "partes.json"), "w", encoding="utf-8") as f:
    json.dump(partes, f, ensure_ascii=False)
total = sum(len(u) for u in unidades)
if unidades and total < 50 * len(unidades) and unidade == "página":
    avisos.append("pouco texto por página: documento pode ser escaneado ou só imagens")
print(json.dumps({"ok": True, "unidade": unidade, "unidades": len(unidades), "chars": total,
                  "partes": len(partes), "metodo": metodo, "ocr": ocr, "avisos": avisos},
                 ensure_ascii=False))
'''


def extract_command(src: str, out_dir: str, *, chunk_chars: int, max_ocr_pages: int) -> str:
    """Linha de shell que roda o script para um documento."""
    return " ".join(
        shlex.quote(p)
        for p in ("python3", SCRIPT_PATH, src, out_dir, str(chunk_chars), str(max_ocr_pages))
    )
