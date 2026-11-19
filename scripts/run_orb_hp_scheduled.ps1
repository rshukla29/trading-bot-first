Set-Location (Split-Path $PSScriptRoot -Parent)  # the repo folder, wherever it was cloned

$logDir = "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir ("orb_hp_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

# No $ErrorActionPreference = "Stop" here: PowerShell 5.1 wraps every stderr
# line from a native process (including Python's normal INFO-level logging,
# which defaults to stderr) in a NativeCommandError. Under "Stop" that
# escalates to a terminating error on the very first log line, killing the
# script before the bot does anything and leaving an empty log file.
#
# ORB-0DTE-HP (SPY 0DTE options on 5-minute EMA/SMA/VWAP/MACD alignment). Runs
# for the whole session (started at 9:30am ET, sells by 3:55pm, exits at
# 4:00pm) — see run_orb_hp_bot.py's own docstring.
& ".\.venv\Scripts\python.exe" -m tradingbot.run_orb_hp_bot *>> $logFile
exit $LASTEXITCODE
