# syntax=docker/dockerfile:1.7

FROM python:3.11-slim

# Instalar UV
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copiar pyproject.toml primeiro (para cache de deps)
COPY pyproject.toml ./

# Instalar PyTorch CPU antes do sentence-transformers para evitar baixar a stack CUDA/NVIDIA
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system torch --index-url https://download.pytorch.org/whl/cpu

# Instalar dependências usando UV com cache persistente entre builds
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system -e .

# Garantir drivers async e pgvector estão presentes
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system asyncpg pgvector "sqlalchemy[asyncio]>=2.0"

# Copiar código fonte
COPY . .

# Regenerar arquivos proto (após copiar o código atualizado)
RUN python -m grpc_tools.protoc --proto_path=proto --python_out=. --grpc_python_out=. proto/neo_desktop.proto

# Copiar entrypoint para dev mode
COPY entrypoint-dev.sh /usr/local/bin/entrypoint-dev.sh
RUN chmod +x /usr/local/bin/entrypoint-dev.sh

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
