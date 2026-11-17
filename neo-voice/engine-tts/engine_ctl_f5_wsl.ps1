# Controle MANUAL do neo-voice F5-TTS engine (pt-br) no WSL2.
# Sobe destacado via WMI (sobrevive ao SSH). NAO auto-inicia no boot.
#   engine_ctl_f5_wsl.ps1 start | stop | status | restart
param([string]$cmd = "status")
$ErrorActionPreference = "SilentlyContinue"

$inner = 'cd /opt/neo-f5 && PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True NEO_F5_PORT=5010 .venv/bin/python neo_f5_engine.py >/opt/neo-f5/f5.log 2>&1'

function Get-Proc {
    Get-CimInstance Win32_Process -Filter "Name='wsl.exe'" | Where-Object { $_.CommandLine -like '*neo_f5_engine*' }
}
function Stop-It {
    wsl -d Ubuntu -u root -- pkill -f neo_f5_engine.py 2>$null
    Get-Proc | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
}
function Ensure-Net {
    # WSL IP muda a cada reboot -> recria portproxy 5010 (host -> WSL) + firewall. Requer admin.
    $wsl = (wsl hostname -I).Trim().Split(' ')[0]
    netsh interface portproxy delete v4tov4 listenport=5010 listenaddress=0.0.0.0 2>$null | Out-Null
    netsh interface portproxy add v4tov4 listenport=5010 listenaddress=0.0.0.0 connectport=5010 connectaddress=$wsl | Out-Null
    if (-not (Get-NetFirewallRule -DisplayName 'neo-voice 5010' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName 'neo-voice 5010' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5010 | Out-Null
    }
    Write-Host "portproxy 5010 -> $wsl"
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
        Write-Host "--- f5.log (tail) ---"
        wsl -d Ubuntu -u root -- tail -n 15 /opt/neo-f5/f5.log 2>$null
    }
}
