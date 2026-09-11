Set-Location "c:\Users\rshuk\.vscode\Projects\trading-bot"

$logDir = "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir ("orb_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

# No $ErrorActionPreference = "Stop" here: PowerShell 5.1 wraps every stderr
# line from a native process (including Python's normal INFO-level logging,
# which defaults to stderr) in a NativeCommandError. Under "Stop" that
# escalates to a terminating error on the very first log line, killing the
# script before the bot does anything and leaving an empty log file.
#
# This script itself runs for the whole trading session (started once
# shortly before 9:30am ET, exits itself around 4:00pm ET) — see
# run_orb_bot.py's own docstring.
& ".\.venv\Scripts\python.exe" -m tradingbot.run_orb_bot *>> $logFile
exit $LASTEXITCODE
