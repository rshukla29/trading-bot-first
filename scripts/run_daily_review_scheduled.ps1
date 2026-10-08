param([string]$ReviewDate = "")

Set-Location "c:\Users\rshuk\.vscode\Projects\trading-bot"

$logDir = "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
# Optional: re-run a past day with -ReviewDate 2026-10-05
$date = if ($ReviewDate) { $ReviewDate } else { Get-Date -Format "yyyy-MM-dd" }
$logFile = Join-Path $logDir ("daily_review_{0}.log" -f $date)

# No $ErrorActionPreference = "Stop" here: PowerShell 5.1 wraps every stderr
# line from a native process in a NativeCommandError, which "Stop" would
# turn into a terminating error on the first log line.
#
# 4:50pm ET, after the 4:30pm bots and snapshot: (1) gather the day's facts
# (read-only), (2) headless Claude Code reads the facts and history and
# replies with a review with recommendations, which this script saves.
# Claude gets read-only tools only — no writing, shell or web.
# Recommendations are for the owner to approve; nothing is applied
# automatically.
& ".\.venv\Scripts\python.exe" -m tradingbot.run_daily_review $date *>> $logFile

$y, $m, $d = $date.Split("-")
$reviewDir = "analysis\daily\$y\$m\$d"
$prompt = (Get-Content "scripts\daily_review_prompt.md" -Raw).Replace("{DATE}", $date).Replace("{YEAR}", $y).Replace("{MONTH}", $m).Replace("{DAY}", $d)
# Read-only tools: Claude reads the facts and history and replies with the
# review; this script saves the reply. Claude cannot write files, run
# commands or reach the web.
$review = & claude -p $prompt --allowedTools "Read" "Glob" "Grep" `
    --disallowedTools "Bash" "PowerShell" "Edit" "Write" "NotebookEdit" "WebFetch" "WebSearch" 2>> $logFile
if ($LASTEXITCODE -eq 0 -and $review) {
    New-Item -ItemType Directory -Force -Path $reviewDir | Out-Null
    ($review -join "`n") | Out-File -FilePath (Join-Path $reviewDir "review_$date.md") -Encoding utf8
} else {
    "Claude review failed (exit $LASTEXITCODE)" | Out-File -Append -FilePath $logFile
}

# Commit locally (no push), like the daily snapshots.
git add "analysis/daily/$y/$m/$d" *>> $logFile
git commit -m "Daily review: $date" *>> $logFile
exit 0
