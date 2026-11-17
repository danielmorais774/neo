# Dependências da imagem do neo-workbench

O sandbox do Neo roda como usuário sem root. **Pacote de sistema só entra por
rebuild desta imagem**, e o `pip install --user` em runtime grava em `/home/neo`,
que não é volume: some no próximo `docker compose up -d`. Então tudo o que uma
skill ou tool usa com frequência mora no `Dockerfile` e está listado aqui.

Fonte de verdade: o [`Dockerfile`](Dockerfile). Este arquivo diz **quem usa** cada
peça, para que um rebuild futuro não derrube uma skill sem ninguém saber.
Conferência automática: [`verificar-dependencias.sh`](verificar-dependencias.sh)
(vai para dentro da imagem como `verificar-dependencias`).

## Recriar a imagem

Na VPS (`ssh -i ~/.ssh/<sua-chave> <usuario>@<ip-da-vps>`):

```sh
cd ~/Documents/projects/neo
docker compose build neo-workbench            # não para nada; o LibreOffice leva alguns minutos
docker compose up -d --no-deps neo-workbench   # troca o container (ver avisos abaixo)
docker exec neo-workbench verificar-dependencias
```

Antes do `up -d`:

- **Jobs em andamento morrem.** O `neo_bash_background` roda dentro deste
  container. Confira que não há job rodando (janela Tarefas do desktop, ou
  `docker exec neo-workbench ps aux`).
- `/workspace` é volume e fica. `/home/neo` (o que o Neo instalou com
  `pip install --user`) vai embora — é esperado.
- O `--no-deps` evita recriar o `neo-workbench-proxy` à toa.

## O que tem e quem usa

### Sistema (apt)

| Pacote | Para quê | Quem usa |
|---|---|---|
| bash, coreutils, procps | shell e `ps` | `neo_bash` em geral |
| git, curl, wget, jq, unzip, zip, p7zip-full | baixar e abrir pacote | `neo_bash` em geral |
| build-essential | compilar wheel sem binário pronto | `pip install --user` em runtime |
| nodejs, npm | scripts JS | `neo_bash` em geral |
| ffmpeg | quadros e áudio de vídeo | skill `ocr-video` (`ffmpeg`, `ffprobe`); tool `neo_transcrever` (`ffprobe`, e `ffmpeg` com `libopus` para tirar o áudio) |
| file | tipo real do arquivo | extração de documento |
| poppler-utils | `pdftotext`, `pdftoppm`, `pdfinfo`, `pdfimages` | `deep_read` (core/documents/extract.py), skill `docx` (`docx_renderizar.py`: PDF → PNG) |
| tesseract-ocr + **-por** + -eng | OCR. Sem o `-por`, nota fiscal escaneada perde acento e cedilha | skill `ocr-video`, `deep_read` (PDF escaneado) |
| **libreoffice-writer-nogui** | `soffice --headless --convert-to pdf` | skill `docx` (`docx_renderizar.py`: .docx → PDF), depois a tool `file_olhar` |
| **fonts-liberation** | métricas iguais às de Arial, Times New Roman e Courier | skill `docx`: sem elas a página renderizada quebra linha em lugar diferente do Word, e o print engana |
| **fonts-crosextra-carlito**, **fonts-crosextra-caladea** | métricas de Calibri e Cambria, o padrão do Word moderno | skill `docx` |
| **fonts-dejavu-core** | fallback com acento e símbolo | skill `docx` |

Em negrito: entrou em 10/11/2026 (session-20cb02b6). O Neo editava .docx às
cegas, conferindo por texto extraído, e entregou um template de faculdade sem o
logo do cabeçalho.

### Python (pip, no sistema)

| Pacote | Quem usa |
|---|---|
| numpy, pandas, sympy, tabulate | `neo_bash` (dados, contas); `deep_read` (planilha); `ocr-video` (numpy) |
| requests, httpx, beautifulsoup4, lxml, html2text, trafilatura | `neo_bash` (web); `deep_read` (HTML) |
| pyyaml, python-dateutil, orjson, chardet, python-magic | `neo_bash` em geral |
| qrcode, pillow, exifread | `neo_bash` (imagem); `ocr-video` (pillow) |
| pypdf, pdfplumber, pymupdf, pdf2image, pytesseract | `deep_read`, `neo_bash` (PDF) |
| **python-docx** | skill `docx` (todos os scripts), `deep_read` (.docx) |
| python-pptx, odfpy, xlrd, openpyxl, xlsxwriter | `deep_read`, `neo_bash` (Office) |
| pyarrow, duckdb | skill `analise-csv`, `neo_bash` (SQL em CSV/Parquet) |

`uv` vem copiado da imagem `ghcr.io/astral-sh/uv`.

## Adicionar uma dependência nova

1. Uma linha no `Dockerfile`, com comentário dizendo para quê (o padrão do arquivo).
2. Uma linha na tabela acima, com **quem usa**.
3. O binário ou módulo no `verificar-dependencias.sh`.
4. Se for de uma skill, o comentário no frontmatter do `SKILL.md` dela aponta para
   este arquivo, e o script dela falha com mensagem clara quando a peça falta (ver
   `skills/docx/scripts/docx_renderizar.py`, que sai com código 3 e cita este
   arquivo).
5. Rebuild e `verificar-dependencias` (acima).
