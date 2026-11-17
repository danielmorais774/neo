# Abre a porta do neo-voice TTS engine no firewall do Windows.
#   powershell -ExecutionPolicy Bypass -File open_firewall.ps1
$port = if ($env:NEO_TTS_PORT) { [int]$env:NEO_TTS_PORT } else { 5006 }
$name = "neo-tts-engine ($port)"
Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $port -Profile Private,Domain | Out-Null
Write-Host "Regra de firewall criada para a porta $port (perfis Private/Domain)."
