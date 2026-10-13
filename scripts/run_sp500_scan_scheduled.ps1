Set-Location "c:\Users\rshuk\.vscode\Projects\trading-bot"

$logDir = "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir ("sp500_scan_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

# No $ErrorActionPreference = "Stop" here: PowerShell 5.1 wraps every stderr
# line from a native process in a NativeCommandError, which "Stop" would
# turn into a terminating error on the first log line.
#
# 4:15pm ET, after the close (final prices): saves the full S&P 500 scan
# (movers, unusual volume, MA-crossover / RSI(2) buy signals) to
# scans/YYYY/MM/DD/sp500_scan_<date>.csv. Read-only; yfinance data only.
& ".\.venv\Scripts\python.exe" -m tradingbot.sp500_scanner --top 20 --save *>> $logFile
exit $LASTEXITCODE
