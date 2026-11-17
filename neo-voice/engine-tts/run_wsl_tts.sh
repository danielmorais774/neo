#!/usr/bin/env bash
# Sobe o neo-voice TTS engine dentro do WSL2 (detachado; mantém o WSL vivo).
#   wsl -d Ubuntu -u root -- bash /opt/neo-tts/run_wsl_tts.sh {start|stop|status}
cd /opt/neo-tts
export NEO_TTS_ATTN=${NEO_TTS_ATTN:-sdpa}
export NEO_TTS_PORT=${NEO_TTS_PORT:-5006}
PY=/opt/neo-tts/.venv/bin/python
LOG=/opt/neo-tts/tts.log

case "${1:-start}" in
  start)
    pkill -f neo_tts_engine.py 2>/dev/null || true
    sleep 1
    : > "$LOG"
    setsid nohup "$PY" neo_tts_engine.py >"$LOG" 2>&1 </dev/null &
    echo "started pid $!"
    ;;
  stop)
    pkill -f neo_tts_engine.py 2>/dev/null && echo "parado" || echo "nao estava rodando"
    ;;
  status)
    if pgrep -f neo_tts_engine.py >/dev/null; then echo "RODANDO ($(pgrep -f neo_tts_engine.py | tr '\n' ' '))"; else echo "PARADO"; fi
    echo "--- tts.log tail ---"; tail -n 15 "$LOG" 2>/dev/null
    ;;
esac
