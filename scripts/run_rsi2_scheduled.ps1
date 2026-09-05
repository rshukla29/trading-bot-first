$ErrorActionPreference = "Stop"
Set-Location "c:\Users\rshuk\.vscode\Projects\trading-bot"

$logDir = "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir ("rsi2_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

& ".\.venv\Scripts\python.exe" -m tradingbot.run_rsi2_bot *>> $logFile
