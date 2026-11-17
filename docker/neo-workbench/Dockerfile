# neo-workbench — o sandbox de execução do próprio Neo (a "bash do Neo").
#
# Container irmão do neo-core: o neo-core executa comandos aqui via docker exec
# (socket já montado), NUNCA pela rede. Roda como usuário não-root, sem acesso
# ao docker.sock, sem secrets do core, com /workspace persistente (volume) onde
# o Neo acumula scripts e projetos entre jobs.
#
# Segurança em camadas (ver docker-compose.yml): cap_drop ALL, no-new-privileges,
# limites de cpu/mem/pids e rede própria (workbench-net) com egress p/ internet;
# o bloqueio de LAN (RFC1918) é aplicado no host via iptables DOCKER-USER.

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Toolset fixo do sandbox. O usuário `neo` não tem root, então NÃO consegue
# `apt install` depois — pacotes de sistema novos entram por rebuild desta imagem.
# Python: pip install --user / venv funcionam normalmente em runtime.
#
# Leitura de documentos (2026-08-30): o sandbox era uma boa oficina de DADOS e
# cego para DOCUMENTO — tinha numpy e pandas e nem `file` para dizer o que é um
# arquivo. Como aqui não há root, binário de sistema SÓ entra por rebuild: não
# adianta o Neo tentar `apt install` no meio da tarefa.
#   file .............. identificar o tipo antes de tentar extrair
#   poppler-utils ..... pdftotext/pdftoppm/pdfimages
#   p7zip-full ........ 7z
#   tesseract-ocr ..... OCR; o pacote -por importa: nota fiscal brasileira
#                       escaneada é imagem dentro de PDF, e sem português o
#                       reconhecimento erra acento e cedilha.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    coreutils \
    procps \
    git \
    curl \
    wget \
    jq \
    unzip \
    zip \
    ca-certificates \
    build-essential \
    nodejs \
    npm \
    ffmpeg \
    file \
    poppler-utils \
    p7zip-full \
    tesseract-ocr \
    tesseract-ocr-por \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# uv para venvs/instalações rápidas de pacotes Python dentro do sandbox
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Libs Python comuns já instaladas (previsível): computação/dados/análise/geração.
# O Neo ainda pode `pip install --user <x>` em runtime (via proxy) para o resto.
RUN pip install --no-cache-dir \
    numpy \
    pandas \
    sympy \
    requests \
    httpx \
    beautifulsoup4 \
    lxml \
    pyyaml \
    python-dateutil \
    tabulate \
    qrcode \
    pillow \
    orjson

# Extração de conteúdo por formato. O `pip install --user` do Neo funciona em
# runtime (o proxy alcança o PyPI), mas grava em /home/neo, que NÃO é volume:
# some no próximo `docker compose up -d`. Então o que ele usa com frequência
# mora aqui, e o resto continua instalável na hora.
#   PDF ....... pypdf (texto), pdfplumber (tabela/layout), pymupdf (rasterizar),
#               pdf2image + pytesseract (o caminho do PDF escaneado)
#   Office .... python-docx, python-pptx, odfpy, xlrd (.xls antigo),
#               openpyxl/xlsxwriter (.xlsx)
#   Entrada ... python-magic (tipo real), html2text/trafilatura (HTML → texto),
#               chardet (encoding) — extrair começa por saber com o que se lida
#   Analítico . pyarrow + duckdb: SQL direto em CSV/Parquet sem carregar tudo na
#               memória, o que importa com mem_limit de 1g
#   Imagem .... exifread (metadado)
RUN pip install --no-cache-dir \
    pypdf \
    pdfplumber \
    pymupdf \
    pdf2image \
    pytesseract \
    python-docx \
    python-pptx \
    odfpy \
    xlrd \
    openpyxl \
    xlsxwriter \
    python-magic \
    html2text \
    trafilatura \
    chardet \
    pyarrow \
    duckdb \
    exifread

# Ver a página (2026-11-10, session-20cb02b6): o Neo editava .docx às cegas — conferia
# por texto extraído e entregou um template de faculdade sem o logo do cabeçalho. A
# skill docx renderiza .docx → PDF (LibreOffice headless) → PNG (pdftoppm, acima) e a
# tool file_olhar manda a página para o modelo de visão. Camada própria para não
# invalidar o cache das de cima.
#   libreoffice-writer-nogui ........ soffice --headless --convert-to pdf, sem X11
#   fonts-liberation ................ métricas de Arial/Times/Courier: sem elas a página
#                                     quebra linha em lugar diferente do Word
#   fonts-crosextra-carlito/caladea . idem para Calibri/Cambria (padrão do Word moderno)
#   fonts-dejavu-core ............... fallback com acento e símbolo
# Quem usa cada peça desta imagem, e como recriar: DEPENDENCIAS.md.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer-nogui \
    fonts-liberation \
    fonts-crosextra-carlito \
    fonts-crosextra-caladea \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# `docker exec neo-workbench verificar-dependencias` depois de todo rebuild.
COPY verificar-dependencias.sh /usr/local/bin/verificar-dependencias
RUN chmod 0755 /usr/local/bin/verificar-dependencias

RUN useradd -m -u 1000 -s /bin/bash neo \
    && mkdir -p /workspace \
    && chown neo:neo /workspace

USER neo
WORKDIR /workspace
ENV HOME=/home/neo \
    PATH=/home/neo/.local/bin:$PATH

# O container fica ocioso; todo trabalho chega via docker exec (com `init: true`
# no compose para colher zumbis de jobs em background).
CMD ["sleep", "infinity"]
