#!/usr/bin/env bash
# Confere, DENTRO do neo-workbench, que a imagem tem o que DEPENDENCIAS.md lista.
#
#   docker exec neo-workbench verificar-dependencias
#   docker exec -i neo-workbench bash < docker/neo-workbench/verificar-dependencias.sh   # imagem antiga
#
# Sai com 1 se faltar qualquer peça. Rode depois de todo rebuild.
set -u

falta=0
ok()  { printf '  ok     %s\n' "$1"; }
nao() { printf '  FALTA  %s  (%s)\n' "$1" "$2"; falta=1; }

echo "Binários:"
for b in bash git curl wget jq unzip zip 7z gcc node npm ffmpeg ffprobe file \
         pdftotext pdftoppm pdfinfo pdfimages tesseract uv soffice fc-list; do
  if command -v "$b" >/dev/null 2>&1; then ok "$b"; else nao "$b" "apt — ver DEPENDENCIAS.md"; fi
done

echo "Idiomas do tesseract:"
for lang in por eng; do
  if tesseract --list-langs 2>/dev/null | grep -qx "$lang"; then ok "tesseract-$lang"; else nao "tesseract-$lang" "tesseract-ocr-$lang"; fi
done

echo "Codificador de áudio (neo_transcrever tira o áudio em opus):"
if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q " libopus "; then ok "ffmpeg libopus"; else nao "ffmpeg libopus" "ffmpeg do Debian traz libopus"; fi

echo "Fontes (render do .docx igual ao Word):"
for f in "Liberation Sans" "Liberation Serif" "Carlito" "Caladea" "DejaVu Sans"; do
  if fc-list 2>/dev/null | grep -qi "$f"; then ok "$f"; else nao "$f" "fonts-liberation / fonts-crosextra-* / fonts-dejavu-core"; fi
done

echo "Módulos Python:"
for m in numpy pandas sympy requests httpx bs4 lxml yaml dateutil tabulate qrcode PIL orjson \
         pypdf pdfplumber fitz pdf2image pytesseract docx pptx odf xlrd openpyxl xlsxwriter magic \
         html2text trafilatura chardet pyarrow duckdb exifread; do
  if python3 -c "import $m" >/dev/null 2>&1; then ok "$m"; else nao "$m" "pip — ver DEPENDENCIAS.md"; fi
done

if command -v soffice >/dev/null 2>&1; then
  echo "LibreOffice converte de verdade:"
  tmp=$(mktemp -d)
  printf 'teste' > "$tmp/t.txt"
  if HOME="$tmp" timeout 120 soffice --headless --norestore "-env:UserInstallation=file://$tmp/perfil" \
       --convert-to pdf --outdir "$tmp" "$tmp/t.txt" >/dev/null 2>&1 && [ -s "$tmp/t.pdf" ]; then
    ok "soffice --convert-to pdf"
  else
    nao "soffice --convert-to pdf" "instalado mas não converte — ver o log do soffice"
  fi
  rm -rf "$tmp"
fi

if [ "$falta" -eq 0 ]; then echo "Tudo certo."; else echo "Falta coisa — a skill que depende dela vai falhar."; fi
exit "$falta"
