# Autoriza a chave do Mac no Windows OpenSSH (porta 22, ja em execucao no PC gamer).
#
# RODE NO WINDOWS, EM UM POWERSHELL COMO ADMINISTRADOR:
#     powershell -ExecutionPolicy Bypass -File authorize-key-windows.ps1
#
# Depois disso, o Mac entra por SSH e chama o WSL2 direto -- sem sshd extra no
# WSL, sem portproxy (que quebrava a cada reboot). O treino do openWakeWord roda
# dentro do WSL atraves dessa conexao.
#
# Pegadinha que este script resolve: se o usuario e ADMIN, o Windows OpenSSH
# IGNORA o ~/.ssh/authorized_keys e so le C:\ProgramData\ssh\
# administrators_authorized_keys -- e ainda exige ACL restrita (so
# Administrators + SYSTEM), senao descarta o arquivo em silencio.

$ErrorActionPreference = "Stop"

$pubkey = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIERhc+veLV38kC1l8mrbnzsQrcPKbkSEXrGuTTGWEZ22 claude-code@mac -> wsl2 pcgamer (openwakeword training)'

# Usuario e admin? Decide qual authorized_keys o sshd vai realmente ler.
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent() `
    ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)

if ($isAdmin) {
    $akFile = "$env:ProgramData\ssh\administrators_authorized_keys"
    Write-Host "usuario e admin -> usando $akFile"
} else {
    $akFile = "$env:USERPROFILE\.ssh\authorized_keys"
    Write-Host "usuario comum -> usando $akFile"
    New-Item -ItemType Directory -Force -Path (Split-Path $akFile) | Out-Null
}

# Adiciona a chave (sem duplicar)
if ((Test-Path $akFile) -and (Select-String -Path $akFile -SimpleMatch -Pattern $pubkey -Quiet)) {
    Write-Host "chave ja estava autorizada."
} else {
    Add-Content -Path $akFile -Value $pubkey
    Write-Host "chave adicionada."
}

# ACL correta (obrigatoria p/ o administrators_authorized_keys; inofensiva no outro)
if ($isAdmin) {
    icacls $akFile /inheritance:r | Out-Null
    icacls $akFile /grant "Administrators:F" | Out-Null
    icacls $akFile /grant "SYSTEM:F" | Out-Null
    Write-Host "ACL ajustada (Administrators + SYSTEM)."
}

# Garante o servico sshd de pe e no boot
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "Pronto. Do Mac, isto deve funcionar:"
Write-Host "  ssh -i ~/.ssh/id_ed25519_pcgamer_wsl $env:USERNAME@192.168.4.185 `"wsl -l -v`""
