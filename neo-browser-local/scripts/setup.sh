#!/usr/bin/env bash
# Registra o host nativo do open-browser-use para a extensão NEO (fork).
#
# Por que não `obu setup`: o upstream só conhece Google Chrome / Chrome Beta, e
# esta máquina usa Brave. E o manifesto precisa listar o ID da NOSSA extensão em
# allowed_origins — o ID do fork é fixo por causa da chave em keys/ (ver README).
#
# Idempotente: rodar de novo só reescreve os manifestos.
set -euo pipefail

HOST_NAME="com.ifuryst.open_browser_use.extension"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT_ID="$(tr -d '[:space:]' < "$ROOT/keys/extension-id.txt")"

command -v obu >/dev/null 2>&1 || {
  echo "erro: 'obu' não está no PATH. Instale com: npm i -g open-browser-use" >&2
  exit 1
}
[ -n "$EXT_ID" ] || { echo "erro: keys/extension-id.txt vazio" >&2; exit 1; }

echo "extensão: $EXT_ID"

# Navegadores Chromium com pasta de perfil nesta máquina.
declare -a ROOTS=(
  "$HOME/Library/Application Support/BraveSoftware/Brave-Browser"
  "$HOME/Library/Application Support/Google/Chrome"
  "$HOME/Library/Application Support/Microsoft Edge"
  "$HOME/Library/Application Support/Chromium"
)

found=0
for root in "${ROOTS[@]}"; do
  [ -d "$root" ] || continue
  dir="$root/NativeMessagingHosts"
  mkdir -p "$dir"
  obu install-manifest --extension-id "$EXT_ID" --output "$dir/$HOST_NAME.json" >/dev/null
  echo "  ✓ $(basename "$root")"
  found=$((found + 1))
done

[ "$found" -gt 0 ] || { echo "erro: nenhum navegador Chromium encontrado" >&2; exit 1; }

cat <<EOF

Falta a parte manual (não dá para automatizar — é clique no navegador):

  1. abra  brave://extensions  (ou chrome://extensions)
  2. ligue "Modo do desenvolvedor"
  3. "Carregar sem compactação" → escolha:
       $ROOT
  4. confirme que o ID aparece como  $EXT_ID
     (se aparecer outro, a chave do manifest.json não foi lida — reinstale)
  5. REINICIE o navegador (o host nativo só é lido no start)

Depois: o ícone da extensão deve ficar com a bolinha verde (● = host conectado).
EOF
