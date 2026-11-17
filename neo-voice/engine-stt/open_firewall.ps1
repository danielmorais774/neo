# Abre a porta do neo-stt engine no firewall do Windows (rode como Administrador).
#   powershell -ExecutionPolicy Bypass -File open_firewall.ps1
$port = if ($env:NEO_STT_PORT) { [int]$env:NEO_STT_PORT } else { 5005 }
$name = "neo-stt-engine ($port)"
Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $port -Profile Private,Domain | Out-Null
Write-Host "Regra de firewall criada para a porta $port (perfis Private/Domain)."
