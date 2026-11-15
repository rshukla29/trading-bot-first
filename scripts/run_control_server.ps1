Set-Location (Split-Path $PSScriptRoot -Parent)  # the repo folder, wherever it was cloned

# The dashboard's Start / Pause / Stop buttons (run_control_server.py).
# The BotControlServer task starts this Monday-Friday at 8am and at Windows
# logon. It runs only on weekdays from 8am to 6pm New York time: it
# rebuilds the dashboard and opens it in the browser (a dashboard tab still
# open from the day before reloads itself instead), and the server stops
# itself at 6pm. The browser is never closed.
# No $ErrorActionPreference = "Stop": see run_orb_scheduled.ps1.
$start = [timespan]"08:00"
$stop = [timespan]"18:00"

$logDir = "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir ("control_server_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

$ny = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$today = [TimeZoneInfo]::ConvertTime((Get-Date), $ny)
$now = $today.TimeOfDay
if ($today.DayOfWeek -in "Saturday", "Sunday" -or $now -lt $start -or $now -ge $stop) {
    Add-Content -Path $logFile -Value ("{0} Outside Mon-Fri 8am-6pm New York - not starting the control server" -f ($today.ToString("yyyy-MM-dd HH:mm:ss")))
    exit 0
}
& ".\.venv\Scripts\python.exe" -m tradingbot.run_control_server --open --until ("{0:hh\:mm}" -f $stop) *>> $logFile
exit $LASTEXITCODE
