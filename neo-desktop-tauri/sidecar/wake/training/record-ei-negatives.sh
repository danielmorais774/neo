#!/usr/bin/env bash
# Grava ~100s da SUA voz real p/ usar como NEGATIVOS duros do wake "ei neo".
# Motivo: o "ei" sintetico (TTS) ja esta no treino e NAO suprime o "ei" da sua
# voz real (domain gap). Precisamos do "ei" REAL como negativo -- espelho do que
# a v2 fez com os positivos.
#
# Uso:  cd neo-desktop-tauri/sidecar/wake/training && ./record-ei-negatives.sh
# Saida: ./neg_real/ei_neg_real.wav  (16kHz mono)  -> me avise o caminho.
#
# Se aparecer pedido de permissao de microfone, ACEITE (Terminal precisa de mic).
set -e
cd "$(dirname "$0")"
OUT_DIR="neg_real"
OUT="$OUT_DIR/ei_neg_real.wav"
mkdir -p "$OUT_DIR"

# device de audio do avfoundation (mic interno = :1). Sobrescreva se necessario:
#   AUDIO_DEV=":2" ./record-ei-negatives.sh
AUDIO_DEV="${AUDIO_DEV:-:1}"
DUR="${DUR:-100}"

echo "==================================================================="
echo " GRAVACAO DE NEGATIVOS REAIS ('ei') -- ${DUR}s"
echo "==================================================================="
echo
echo " Fale NA SUA voz e intensidade normais. Roteiro (siga os avisos):"
echo "   0-35s : diga 'ei' ISOLADO, ~1x a cada 2s, variando forca/tom"
echo "   35-65s: 'ei' em FRASES ('ei, vem ca', 'ei, olha isso', 'ei, cara')"
echo "   65-100s: FALE ALEATORIO + confusores:"
echo "            'ne o', 'meio', 'enio', 'video', 'e o', 'espontaneo',"
echo "            'que horas sao', 'liga a luz', 'tudo bem com voce'"
echo
echo " NAO diga 'ei neo' completo aqui (isso e positivo, nao negativo)."
echo
read -r -p " Enter p/ comecar (3s de contagem)... " _
for i in 3 2 1; do printf "   %s...\r" "$i"; sleep 1; done
echo "   GRAVANDO!        "

# prompts temporizados em background
( sleep 35;  echo ">>> [35s] agora 'ei' em FRASES";  \
  sleep 30;  echo ">>> [65s] agora FALA ALEATORIA + confusores" ) &
PROMPT_PID=$!

ffmpeg -hide_banner -loglevel error -f avfoundation -i "$AUDIO_DEV" \
  -t "$DUR" -ac 1 -ar 16000 -sample_fmt s16 -y "$OUT"

kill "$PROMPT_PID" 2>/dev/null || true
echo
echo "==================================================================="
echo " OK -> $(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
DUR_REAL=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT" 2>/dev/null || echo "?")
echo " duracao: ${DUR_REAL}s | formato: 16kHz mono s16"
echo " Me passe esse caminho que eu levo pro PC gamer e retreino."
echo "==================================================================="
