#!/usr/bin/env bash
# Habilita SSH no WSL2 do PC gamer para o pipeline de treino do openWakeWord.
#
# RODE ESTE SCRIPT DENTRO DO WSL2 (Ubuntu) do PC gamer:
#     bash setup-wsl-ssh.sh
#
# Depois rode o bloco de PowerShell que ele imprime no final, COMO ADMIN, no
# Windows -- sem isso o WSL2 nao e alcancavel da LAN (mesmo gotcha do netpro
# proxy das engines de TTS: o WSL2 tem IP proprio e o portproxy quebra a cada
# reboot do Windows).
set -euo pipefail

PORT=2222
PUBKEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIERhc+veLV38kC1l8mrbnzsQrcPKbkSEXrGuTTGWEZ22 claude-code@mac -> wsl2 pcgamer (openwakeword training)'

echo "==> instalando openssh-server"
sudo apt-get update -qq
sudo apt-get install -y -qq openssh-server

echo "==> configurando sshd na porta $PORT (sem senha, so chave)"
sudo tee /etc/ssh/sshd_config.d/neo-openwakeword.conf >/dev/null <<EOF
Port $PORT
PasswordAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
EOF

echo "==> autorizando a chave do Mac"
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
grep -qF "${PUBKEY%% *} ${PUBKEY#* }" ~/.ssh/authorized_keys 2>/dev/null \
  || echo "$PUBKEY" >> ~/.ssh/authorized_keys

echo "==> subindo o sshd"
sudo ssh-keygen -A                       # gera host keys se faltarem
sudo service ssh restart || sudo service ssh start

WSL_IP=$(hostname -I | awk '{print $1}')
echo
echo "sshd ouvindo na porta $PORT; IP do WSL2 = $WSL_IP"
echo
echo "================================================================"
echo " AGORA, NO WINDOWS, EM UM POWERSHELL COMO ADMINISTRADOR:"
echo "================================================================"
cat <<EOF

# 1) encaminha a porta $PORT do Windows -> WSL2
netsh interface portproxy delete v4tov4 listenport=$PORT listenaddress=0.0.0.0 2>\$null
netsh interface portproxy add v4tov4 listenport=$PORT listenaddress=0.0.0.0 \`
    connectport=$PORT connectaddress=$WSL_IP

# 2) libera no firewall do Windows
New-NetFirewallRule -DisplayName "WSL2 SSH (Neo openWakeWord)" \`
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort $PORT -ErrorAction SilentlyContinue

# 3) confere
netsh interface portproxy show v4tov4

EOF
echo "================================================================"
echo "IMPORTANTE: o IP do WSL2 MUDA a cada reboot do Windows."
echo "Se o SSH parar de responder depois de reiniciar, rode o passo 1 de novo"
echo "com o IP novo (hostname -I dentro do WSL2)."
echo "================================================================"
