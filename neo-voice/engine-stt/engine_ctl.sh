#!/usr/bin/env bash
# Controle do neo-stt engine na variante CPU (LXC parakeet-cpu).
# Mesma interface do engine_ctl.ps1 do PC gamer, para o painel do
# neo-desktop-tauri poder tratar os dois engines do mesmo jeito:
#   engine_ctl.sh start|stop|restart|status
set -euo pipefail

UNIT=neo-stt-cpu

case "${1:-status}" in
  start)   systemctl start   "$UNIT"; systemctl --no-pager status "$UNIT" | head -4 ;;
  stop)    systemctl stop    "$UNIT"; echo "parado." ;;
  restart) systemctl restart "$UNIT"; systemctl --no-pager status "$UNIT" | head -4 ;;
  status)
    if systemctl is-active --quiet "$UNIT"; then
      echo "RODANDO: PID $(systemctl show -p MainPID --value "$UNIT")"
    else
      echo "PARADO"
    fi
    echo "--- journal (tail) ---"
    journalctl -u "$UNIT" -n 20 --no-pager -o cat || true
    ;;
  *) echo "uso: $0 {start|stop|restart|status}" >&2; exit 2 ;;
esac
