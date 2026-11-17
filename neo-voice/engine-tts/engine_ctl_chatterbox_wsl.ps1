# Controle MANUAL do neo-voice Chatterbox Multilingual engine no WSL2.
# Sobe destacado via WMI (sobrevive ao SSH). NAO auto-inicia no boot.
#   engine_ctl_chatterbox_wsl.ps1 start | stop | status | restart
param([string]$cmd = "status")
$ErrorActionPreference = "SilentlyContinue"

$inner = 'cd /opt/neo-chatterbox && PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True NEO_CB_PORT=5011 .venv/bin/python neo_chatterbox_engine.py >/opt/neo-chatterbox/cb.log 2>&1'

function Get-Proc {
    Get-CimInstance Win32_Process -Filter "Name='wsl.exe'" | Where-Object { $_.CommandLine -like '*neo_chatterbox_engine*' }
}
function Stop-It {
    wsl -d Ubuntu -u root -- pkill -f neo_chatterbox_engine.py 2>$null
    Get-Proc | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
}
function Ensure-Net {
    # WSL IP muda a cada reboot -> recria portproxy 5011 (host -> WSL) + firewall. Requer admin.
    $wsl = (wsl hostname -I).Trim().Split(' ')[0]
    netsh interface portproxy delete v4tov4 listenport=5011 listenaddress=0.0.0.0 2>$null | Out-Null
    netsh interface portproxy add v4tov4 listenport=5011 listenaddress=0.0.0.0 connectport=5011 connectaddress=$wsl | Out-Null
    if (-not (Get-NetFirewallRule -DisplayName 'neo-voice 5011' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName 'neo-voice 5011' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5011 | Out-Null
    }
    Write-Host "portproxy 5011 -> $wsl"
}
function Start-It {
    Ensure-Net
    $cl = 'wsl -d Ubuntu -u root -- bash -lc "' + $inner + '"'
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cl }
    Write-Host "Win32_Process.Create ReturnValue=$($r.ReturnValue) PID=$($r.ProcessId)"
}

switch ($cmd.ToLower()) {
    "start"   { Stop-It; Start-Sleep 2; Start-It }
    "stop"    { Stop-It; Write-Host "parado" }
    "restart" { Stop-It; Start-Sleep 2; Start-It }
    default {
        if (Get-Proc) { Write-Host "RODANDO" } else { Write-Host "PARADO" }
        Write-Host "--- cb.log (tail) ---"
        wsl -d Ubuntu -u root -- tail -n 15 /opt/neo-chatterbox/cb.log 2>$null
    }
}
