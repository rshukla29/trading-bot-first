Set-Location (Split-Path $PSScriptRoot -Parent)  # the repo folder, wherever it was cloned

# The dashboard's Start / Pause / Stop buttons (run_control_server.py).
# Started at logon by the BotControlServer task and runs until logoff; it
# listens on 127.0.0.1 only. No $ErrorActionPreference = "Stop": see
# run_orb_scheduled.ps1.
$logDir = "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir ("control_server_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
& ".\.venv\Scripts\python.exe" -m tradingbot.run_control_server *>> $logFile
exit $LASTEXITCODE
