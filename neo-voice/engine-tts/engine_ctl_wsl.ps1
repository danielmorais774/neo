# Controle MANUAL do neo-voice TTS engine rodando no WSL2 (Qwen3-TTS).
# Sobe destacado via WMI (sobrevive ao fechar a sessao SSH). NAO auto-inicia no boot.
#   engine_ctl_wsl.ps1 start | stop | status | restart
param([string]$cmd = "status")
$ErrorActionPreference = "SilentlyContinue"

$inner = 'cd /opt/neo-tts && PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True NEO_TTS_ATTN=sdpa NEO_TTS_PORT=5006 .venv/bin/python neo_tts_engine.py >/opt/neo-tts/tts.log 2>&1'

function Get-Proc {
    Get-CimInstance Win32_Process -Filter "Name='wsl.exe'" | Where-Object { $_.CommandLine -like '*neo_tts_engine*' }
}
function Stop-It {
    wsl -d Ubuntu -u root -- pkill -f neo_tts_engine.py 2>$null
    Get-Proc | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
}
function Start-It {
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
        Write-Host "--- tts.log (tail) ---"
        wsl -d Ubuntu -u root -- tail -n 15 /opt/neo-tts/tts.log 2>$null
    }
}
