FROM python:3.11-slim

# Instalar UV
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copiar pyproject.toml primeiro (para cache de deps)
COPY pyproject.toml ./

# Instalar dependências usando UV
RUN uv pip install --system -e .

# Garantir drivers async e pgvector estão presentes
RUN uv pip install --system asyncpg pgvector "sqlalchemy[asyncio]>=2.0"

# Copiar código fonte
COPY . .

# Copiar entrypoint para dev mode
COPY entrypoint-dev.sh /usr/local/bin/entrypoint-dev.sh
RUN chmod +x /usr/local/bin/entrypoint-dev.sh

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
