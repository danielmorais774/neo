#!/bin/bash
set -e

echo "📦 Installing dependencies from mounted volume..."
cd /app
uv pip install --system -e . 2>&1 | tail -5

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
