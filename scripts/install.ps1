# One-time setup on a new Windows PC (see docs/SETUP.md).
#
# Right-click this file > "Run with PowerShell", or from a PowerShell window
# in the repo folder:
#     powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#
# It checks Python and Git, installs the bot's Python packages into .venv,
# creates .env from .env.example (if there isn't one), runs the tests, and
# creates the scheduled tasks (New York times converted to this PC's clock).
# Safe to run again: it keeps an existing .env and replaces the tasks.
#
#     -SkipTasks    install only, don't touch Task Scheduler

param([switch]$SkipTasks)

# No $ErrorActionPreference = "Stop": PowerShell 5.1 turns normal stderr
# output of python/pip/git into errors. Exit codes are checked instead.
$repo = Split-Path $PSScriptRoot -Parent
Set-Location $repo

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "   OK  $text" -ForegroundColor Green }
function Fail($text) {
    Write-Host "   PROBLEM  $text" -ForegroundColor Red
    Write-Host ""
    Read-Host "Fix that, then run this again. Press Enter to close"
    exit 1
}

Step "Checking Python and Git"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Fail "Python not found. Install Python 3.11 or newer from python.org and tick 'Add python.exe to PATH', then open a new window."
}
$ver = (& python -c "import sys; print('%d.%d' % sys.version_info[:2])") 2>$null
if (-not $ver -or [version]$ver -lt [version]"3.11") {
    Fail "Python $ver found, but 3.11 or newer is needed (python.org)."
}
Ok "Python $ver"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "Git not found. Install Git for Windows from git-scm.com, then open a new window."
}
Ok "Git"
$gitName = (& git config user.name) 2>$null
if (-not $gitName) {
    Write-Host "   Git needs your name and email once (the reports are saved as Git commits)."
    $n = Read-Host "   Your name"
    $e = Read-Host "   Your email"
    & git config --global user.name $n
    & git config --global user.email $e
}
Ok "Git name set"

Step "Installing the bot's Python packages (a few minutes the first time)"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail "Could not create .venv" }
}
& .venv\Scripts\python.exe -m pip install --quiet --upgrade pip 2>$null
& .venv\Scripts\python.exe -m pip install --quiet -e ".[dev]"
if ($LASTEXITCODE -ne 0) { Fail "Installing packages failed (see the messages above). Check the internet connection." }
Ok "Packages installed"

Step "Settings file (.env)"
if (Test-Path ".env") {
    Ok ".env already exists - kept as it is"
} else {
    Copy-Item ".env.example" ".env"
    Ok "Created .env from .env.example - review it in Notepad (docs/SETUP.md, step 5)"
}
New-Item -ItemType Directory -Force -Path "data", "logs", "reports" | Out-Null

Step "Running the tests"
& .venv\Scripts\python.exe -m pytest -q 2>&1 | Select-Object -Last 3
if ($LASTEXITCODE -ne 0) { Fail "Some tests failed. Don't run the bots until they pass." }
Ok "All tests passed"

if ($SkipTasks) {
    Step "Skipping Task Scheduler (-SkipTasks)"
} else {
    Step "Creating the scheduled tasks (Mon-Fri)"
    # Name = New York time, script. Scripts that aren't in this copy of the repo are skipped.
    $tasks = [ordered]@{
        SnapshotPremarketBot  = "09:00", "run_snapshot_premarket.ps1"
        StepStopTradingBot    = "09:25", "run_step_stop_scheduled.ps1"
        ORBTradingBot         = "09:30", "run_orb_scheduled.ps1"
        StockOrbTradingBot    = "09:30", "run_stock_orb_scheduled.ps1"
        TrendJoinTradingBot   = "09:50", "run_trend_join_scheduled.ps1"
        CreditOrbTradingBot   = "10:20", "run_credit_orb_scheduled.ps1"
        IronCondorTradingBot  = "15:45", "run_iron_condor_scheduled.ps1"
        ExpiryGuardBot        = "15:57", "run_expiry_guard_scheduled.ps1"
        SP500ScanBot          = "16:15", "run_sp500_scan_scheduled.ps1"
        RSI2TradingBot        = "16:30", "run_rsi2_scheduled.ps1"
        SnapshotPostmarketBot = "16:30", "run_snapshot_postmarket.ps1"
        DailyReviewBot        = "16:50", "run_daily_review_scheduled.ps1"
    }
    $ny = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    $days = "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 72) -StartWhenAvailable
    foreach ($name in $tasks.Keys) {
        $nyTime, $script = $tasks[$name]
        $path = Join-Path $repo "scripts\$script"
        if (-not (Test-Path $path)) { continue }
        $nyClock = [datetime]::SpecifyKind([datetime]::Today.Add([timespan]$nyTime), "Unspecified")
        $local = [TimeZoneInfo]::ConvertTime($nyClock, $ny, [TimeZoneInfo]::Local)
        $action = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$path`""
        $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $local
        Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
        Ok ("{0,-22} {1} New York = {2} on this PC" -f $name, $nyTime, $local.ToString("HH:mm"))
    }
    if ([TimeZoneInfo]::Local.Id -ne $ny.Id) {
        Write-Host "   This PC isn't on New York time. If your clock changes for daylight saving on"
        Write-Host "   different dates than the US, run this again after each change."
    }
}

Step "Done"
Write-Host "   Next: TWS settings and the first check - docs/SETUP.md, steps 5 to 7."
Read-Host "Press Enter to close"
