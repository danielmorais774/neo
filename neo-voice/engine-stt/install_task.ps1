# Registra (e inicia) o neo-stt engine como Scheduled Task -> autostart no logon
# e independente da sessao SSH. Roda na sessao interativa do usuario (tem GPU).
#   powershell -ExecutionPolicy Bypass -File install_task.ps1
$ErrorActionPreference = "Stop"
$dir  = $PSScriptRoot
$cmd  = Join-Path $dir "run_engine.cmd"
$name = "neo-stt-engine"

$action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument ('/c "{0}"' -f $cmd) -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -Hidden
$principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force | Out-Null
Write-Host "Task '$name' registrada (autostart no logon)."
Start-ScheduledTask -TaskName $name
Write-Host "Task iniciada."
