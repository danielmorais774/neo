# Controle do neo-voice TTS engine no PC gamer (Qwen3-TTS).
#   engine_ctl.ps1 start    -> sobe destacado (sobrevive ao SSH, via WMI), loga em tts.log
#   engine_ctl.ps1 stop
#   engine_ctl.ps1 status
#   engine_ctl.ps1 restart
param([string]$cmd = "status")

$ErrorActionPreference = "SilentlyContinue"
$dir = $PSScriptRoot
$log = Join-Path $dir "tts.log"

function Get-EngineProcs {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*neo_tts_engine*" }
}
function Stop-Engine {
    Get-EngineProcs | ForEach-Object { Write-Host "Parando PID $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force }
}
function Start-Engine {
    Remove-Item $log -ErrorAction SilentlyContinue
    $cmdfile = Join-Path $dir "run_tts.cmd"
    $cl = 'cmd.exe /c "' + $cmdfile + '"'
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cl; CurrentDirectory = $dir }
    Write-Host "Win32_Process.Create ReturnValue=$($r.ReturnValue) PID=$($r.ProcessId)"
}

switch ($cmd.ToLower()) {
    "start"   { Stop-Engine; Start-Engine }
    "stop"    { Stop-Engine; Write-Host "parado." }
    "restart" { Stop-Engine; Start-Sleep 1; Start-Engine }
    default {
        $p = Get-EngineProcs
        if ($p) { Write-Host "RODANDO: PID $($p.ProcessId)" } else { Write-Host "PARADO" }
        Write-Host "--- tts.log (tail) ---"
        Get-Content $log -Tail 20 -ErrorAction SilentlyContinue
    }
}
