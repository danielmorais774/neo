"""Renderiza páginas de um .docx em PNG, para OLHAR antes de entregar.

    python3 /skills/docx/scripts/docx_renderizar.py /workspace/files/saida.docx [--paginas 1-2]

.docx → PDF (LibreOffice headless) → PNG (pdftoppm). Imprime JSON com os PNGs.
Depois: `file_olhar(imagens=[...], pergunta=...)` descreve o que está na página —
o texto extraído não mostra logo sumido, campo vazio, alinhamento torto nem
"parece que só jogou e deixou lá".

Depende do LibreOffice NA IMAGEM do workbench (não dá para instalar em runtime:
o sandbox não tem root). Sem ele, sai com código 3 e diz onde está a receita:
docker/neo-workbench/DEPENDENCIAS.md.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _soffice() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def renderizar(arquivo: str, primeira: int = 1, ultima: int = 2, saida: str | None = None,
               dpi: int = 70) -> dict:
    origem = Path(arquivo)
    if not origem.exists():
        return {"ok": False, "erro": f"arquivo não existe: {arquivo}"}
    soffice = _soffice()
    if soffice is None:
        return {
            "ok": False,
            "erro": (
                "LibreOffice não está na imagem do workbench — não dá para renderizar. "
                "Receita: docker/neo-workbench/DEPENDENCIAS.md (rebuild da imagem). "
                "Siga sem o print, mas diga a ele que não olhou a página."
            ),
            "codigo": 3,
        }
    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm is None:
        return {"ok": False, "erro": "pdftoppm (poppler-utils) ausente na imagem", "codigo": 3}

    destino = Path(saida) if saida else origem.parent / f".render-{origem.stem}"
    destino.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lo-") as casa:
        # Perfil próprio: o LibreOffice trava/recusa quando dois processos dividem o
        # perfil padrão, e o HOME do sandbox não é volume.
        env = {**os.environ, "HOME": casa}
        proc = subprocess.run(
            [soffice, "--headless", "--norestore", f"-env:UserInstallation=file://{casa}/perfil",
             "--convert-to", "pdf", "--outdir", str(destino), str(origem)],
            capture_output=True, text=True, timeout=180, env=env,
        )
    pdf = destino / f"{origem.stem}.pdf"
    if proc.returncode != 0 or not pdf.exists():
        return {"ok": False, "erro": f"conversão falhou: {(proc.stderr or proc.stdout)[-400:]}"}

    total = None
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo:
        info = subprocess.run([pdfinfo, str(pdf)], capture_output=True, text=True, timeout=30)
        for linha in info.stdout.splitlines():
            if linha.startswith("Pages:"):
                total = int(linha.split()[-1])
    if total:
        ultima = min(ultima, total)
    prefixo = destino / "pagina"
    for antigo in destino.glob("pagina-*.png"):
        antigo.unlink()
    subprocess.run(
        [pdftoppm, "-png", "-r", str(dpi), "-f", str(primeira), "-l", str(ultima), str(pdf), str(prefixo)],
        check=True, capture_output=True, timeout=120,
    )
    paginas = sorted(str(p) for p in destino.glob("pagina-*.png"))
    return {"ok": bool(paginas), "pdf": str(pdf), "paginas": paginas, "total_paginas": total}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("arquivo")
    ap.add_argument("--paginas", default="1-2", help="intervalo, ex.: 1-2 ou 3")
    ap.add_argument("--saida", help="pasta dos PNGs (padrão: .render-<nome> ao lado do arquivo)")
    ap.add_argument("--dpi", type=int, default=70)
    a = ap.parse_args()
    ini, _, fim = a.paginas.partition("-")
    r = renderizar(a.arquivo, int(ini), int(fim or ini), a.saida, a.dpi)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r.get("ok") else int(r.get("codigo", 1))


if __name__ == "__main__":
    raise SystemExit(main())
