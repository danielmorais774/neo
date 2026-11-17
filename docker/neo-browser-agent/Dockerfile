# neo-browser-agent — o corpo do subagente de browser (docs/browser-agent-plan.md)
#
# Só DEPENDÊNCIAS ficam na imagem; o código do wrapper (./app) é bind-mounted no
# compose, então editar app/*.py + `docker restart neo-browser-agent` basta —
# sem rebuild. browser-use 0.13 fala CDP direto (cdp-use, sem Playwright), por
# isso o Chromium vem do apt e é apontado por executable_path.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium fonts-liberation fonts-noto-color-emoji ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir browser-use==0.13.4 "fastapi>=0.110" "uvicorn[standard]>=0.29"

# Chromium não roda como root sem --no-sandbox; rodamos como usuário comum E
# mesmo assim passamos --no-sandbox (sem userns no container). /data/profiles é
# o volume dos perfis persistentes (cookies/logins).
RUN useradd -m -u 1000 neo && mkdir -p /data/profiles && chown -R neo:neo /data
USER neo

ENV NEO_BROWSER_CHROMIUM=/usr/bin/chromium \
    ANONYMIZED_TELEMETRY=false \
    PYTHONUNBUFFERED=1

WORKDIR /app
EXPOSE 5100
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5100"]
