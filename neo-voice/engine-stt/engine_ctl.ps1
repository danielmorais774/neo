# Controle do neo-stt engine no PC gamer.
#   engine_ctl.ps1 start    -> sobe o engine destacado (sobrevive ao SSH, via WMI)
#   engine_ctl.ps1 stop     -> mata o engine
#   engine_ctl.ps1 status   -> mostra processo + ultimas linhas do log
#   engine_ctl.ps1 restart
param([string]$cmd = "status")

$ErrorActionPreference = "SilentlyContinue"
$dir = $PSScriptRoot
$pyw = Join-Path $dir ".venv\Scripts\pythonw.exe"
$py  = Join-Path $dir ".venv\Scripts\python.exe"
$script = Join-Path $dir "neo_stt_engine.py"
$log = Join-Path $dir "engine.log"

function Get-EngineProcs {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*neo_stt_engine*" }
}

function Stop-Engine {
    Get-EngineProcs | ForEach-Object {
        Write-Host "Parando PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force
    }
}

function Start-Engine {
    Remove-Item $log -ErrorAction SilentlyContinue
    # usa python.exe (NAO pythonw: sob pythonw sys.stdout/err sao None e o uvicorn
    # falha). cmd /c com redirect da ao python handles validos -> engine.log.
    $cmdfile = Join-Path $dir "run_engine.cmd"
    $cl = 'cmd.exe /c "' + $cmdfile + '"'
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine      = $cl
        CurrentDirectory = $dir
    }
    Write-Host "Win32_Process.Create ReturnValue=$($r.ReturnValue) PID=$($r.ProcessId)"
}

switch ($cmd.ToLower()) {
    "start"   { Stop-Engine; Start-Engine }
    "stop"    { Stop-Engine; Write-Host "parado." }
    "restart" { Stop-Engine; Start-Sleep 1; Start-Engine }
    default {
        $p = Get-EngineProcs
        if ($p) { Write-Host "RODANDO: PID $($p.ProcessId)" } else { Write-Host "PARADO" }
        Write-Host "--- engine.log (tail) ---"
        Get-Content $log -Tail 20 -ErrorAction SilentlyContinue
    }
}
