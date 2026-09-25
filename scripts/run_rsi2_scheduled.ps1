Set-Location "c:\Users\rshuk\.vscode\Projects\trading-bot"

$logDir = "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$date = Get-Date -Format "yyyy-MM-dd"
$logFile = Join-Path $logDir ("rsi2_{0}.log" -f $date)
$maLogFile = Join-Path $logDir ("ma_crossover_{0}.log" -f $date)

# No $ErrorActionPreference = "Stop" here: PowerShell 5.1 wraps every stderr
# line from a native process (including Python's normal INFO-level logging,
# which defaults to stderr) in a NativeCommandError. Under "Stop" that
# escalates to a terminating error on the very first log line, killing the
# script before the bot does anything and leaving an empty log file.
& ".\.venv\Scripts\python.exe" -m tradingbot.run_rsi2_bot *>> $logFile
$rsi2Exit = $LASTEXITCODE

# MA-crossover runs right after, in the same task, rather than as its own
# 4:30pm task: both bots use IBKR client ID 1 (their orders must stay
# manageable by the ID that placed them), and two simultaneous connections
# on one ID fail with error 326. Runs even if RSI2 failed.
& ".\.venv\Scripts\python.exe" -m tradingbot.run_ibkr_bot *>> $maLogFile
$maExit = $LASTEXITCODE

if ($rsi2Exit -ne 0) { exit $rsi2Exit }
exit $maExit
