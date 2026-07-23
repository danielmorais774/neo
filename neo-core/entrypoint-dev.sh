#!/bin/bash
set -e

cd /app

STATE_DIR="/tmp/neo-core-dev"
HASH_FILE="$STATE_DIR/pyproject.sha256"
mkdir -p "$STATE_DIR"

CURRENT_HASH="$(sha256sum pyproject.toml | awk '{print $1}')"
PREV_HASH=""
if [ -f "$HASH_FILE" ]; then
  PREV_HASH="$(cat "$HASH_FILE")"
fi

if [ "$CURRENT_HASH" != "$PREV_HASH" ]; then
  echo "📦 pyproject changed, syncing dependencies..."
  uv pip install --system -e . 2>&1 | tail -20
  echo "$CURRENT_HASH" > "$HASH_FILE"
else
  echo "📦 pyproject unchanged, skipping dependency sync."
fi

echo "🔍 Checking installation..."
python -c "import api.main; print('✅ api.main import OK')" || echo "❌ api.main import FAILED"

echo "🚀 Starting uvicorn with hot-reload..."
# --timeout-graceful-shutdown: without it the reload HANGS at "Waiting for background
# tasks to complete" (the in-process gRPC server + open desktop streams never finish),
# leaving neo-core down on every code change. Bounding it to 5s makes --reload actually
# apply edits without a manual `docker restart`. See neo-music-feature.
# O log vai para o arquivo E para o stdout do container por PROCESS SUBSTITUTION,
# não por pipe. Com `exec uvicorn ... | tee`, o exec NÃO substitui o PID 1: o
# shell precisa ficar vivo para manter o pipeline, então quando o uvicorn morre
# (aconteceu: corrupção de heap do lxml, ver docs/deep-research-v2-plan.md §12.6)
# o `tee` segue de pé e o container continua "Up" MENTINDO que está saudável —
# a API fica fora do ar até alguém reiniciar na mão. Com `> >(tee ...)` o uvicorn
# é o PID 1 de verdade: se ele morre, o container morre e a restart policy sobe
# tudo de novo.
exec uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload \
  --timeout-graceful-shutdown 5 \
  --reload-dir /app/api \
  --reload-dir /app/core \
  --reload-dir /app/shared \
  --reload-dir /app/modules \
  --reload-dir /app/db \
  --reload-dir /app/grpc_server.py \
  > >(tee -a /app/logs/app.log) 2>&1
