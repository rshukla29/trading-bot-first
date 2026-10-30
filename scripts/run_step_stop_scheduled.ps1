Set-Location (Split-Path $PSScriptRoot -Parent)  # the repo folder, wherever it was cloned

$logDir = "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir ("step_stop_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

# No $ErrorActionPreference = "Stop" here: PowerShell 5.1 wraps every stderr
# line from a native process (including Python's normal INFO-level logging,
# which defaults to stderr) in a NativeCommandError. Under "Stop" that
# escalates to a terminating error on the very first log line, killing the
# script before the bot does anything and leaving an empty log file.
#
# This script itself runs for the whole trading session (started once
# around 9:25am ET, waits for the 9:30 open, exits itself at 4:00pm ET) — see
# run_step_stop_bot.py's own docstring.
& ".\.venv\Scripts\python.exe" -m tradingbot.run_step_stop_bot *>> $logFile
exit $LASTEXITCODE
