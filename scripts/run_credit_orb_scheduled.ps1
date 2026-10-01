Set-Location "c:\Users\rshuk\.vscode\Projects\trading-bot"

$logDir = "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir ("credit_orb_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

# No $ErrorActionPreference = "Stop" here: PowerShell 5.1 wraps every stderr
# line from a native process (including Python's normal INFO-level logging,
# which defaults to stderr) in a NativeCommandError. Under "Stop" that
# escalates to a terminating error on the very first log line, killing the
# script before the bot does anything and leaving an empty log file.
#
# Strategy #6, ORB credit spreads (SPY 0DTE). Started 10:20am ET; the 60-min
# opening range completes at 10:30, entries until 12:00, spread bought back
# at 3:50pm, process exits at 4:00pm (see run_credit_orb_bot.py).
& ".\.venv\Scripts\python.exe" -m tradingbot.run_credit_orb_bot *>> $logFile
exit $LASTEXITCODE
