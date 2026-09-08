$ErrorActionPreference = "Stop"
Set-Location "c:\Users\rshuk\.vscode\Projects\trading-bot"

$logDir = "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir ("iron_condor_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

& ".\.venv\Scripts\python.exe" -m tradingbot.run_iron_condor_bot *>> $logFile
