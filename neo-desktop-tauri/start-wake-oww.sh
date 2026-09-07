#!/usr/bin/env bash
# Sobe o Neo Desktop com o wake word do openWakeWord (ei_neo.onnx) no lugar do
# detector antigo (EfficientWord-Net). Teste real do modelo treinado na 5070 Ti.
#
#   ./start-wake-oww.sh              # limiar padrao 0.6
#   NEO_WAKE_THRESHOLD=0.7 ./start-wake-oww.sh   # mais conservador (menos FP, menos recall)
#   NEO_WAKE_THRESHOLD=0.5 ./start-wake-oww.sh   # mais sensivel (mais recall, mais FP)
#
# Para VOLTAR ao detector antigo: rode `yarn start` normal (sem este script).
# Nada e permanente -- so variaveis de ambiente; o Rust ja as le (services/wake.rs).
set -e
cd "$(dirname "$0")"

export NEO_WAKE_SCRIPT="$(pwd)/sidecar/wake/neo_wake_oww.py"
export NEO_WAKE_PYTHON="$(pwd)/sidecar/wake/.venv-eff/bin/python"
# Limiar do openWakeWord (0..1). A voz real dispara em ~0.6-0.98; os confusores
# ("ne o", "-aneo") sobem ate ~0.9, entao mexer aqui e o principal ajuste do teste.
export NEO_WAKE_THRESHOLD="${NEO_WAKE_THRESHOLD:-0.6}"
# Frames consecutivos acima do limiar p/ confirmar (debounce anti-spike).
export NEO_WAKE_OWW_FRAMES="${NEO_WAKE_OWW_FRAMES:-2}"

echo "== Neo Desktop + openWakeWord =="
echo "   script:    $NEO_WAKE_SCRIPT"
echo "   threshold: $NEO_WAKE_THRESHOLD  (frames=$NEO_WAKE_OWW_FRAMES)"
echo "   (barge-in 'pare' segue no EfficientWord-Net)"
echo "   voltar ao antigo: yarn start"
echo
exec yarn start
