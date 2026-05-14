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
exec uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload \
  --reload-dir /app/api \
  --reload-dir /app/core \
  --reload-dir /app/shared \
  --reload-dir /app/modules \
  --reload-dir /app/db \
  --reload-dir /app/grpc_server.py
