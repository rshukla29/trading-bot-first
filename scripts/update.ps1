# Installs a new release (a vX.Y.Z tag). Started by the dashboard's Update now
# button (updates.start_update); can also be run by hand:
#     powershell -ExecutionPolicy Bypass -File scripts\update.ps1            # newest release
#     powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Version v1.2.0
#
# Never touches .env, data\ (trade diary, controls) or the reports. Steps:
# fast-forward to the release, reinstall the Python packages, check the code
# loads, re-register the scheduled tasks, restart the control server.
# If any step fails it goes back to the previous version.
# The outcome is written to data\update_result.json (shown on the dashboard).
# -NoTasks / -NoRestart: for trying it on a spare copy, without touching this
# PC's scheduled tasks or the running control server.
param([string]$Version = "", [switch]$NoTasks, [switch]$NoRestart)

Set-Location (Split-Path $PSScriptRoot -Parent)  # the repo folder
$env:GIT_TERMINAL_PROMPT = "0"
$py = ".\.venv\Scripts\python.exe"

function Say($text) { Write-Output ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $text) }
function Version { (& $py -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])").Trim() }
function Result($ok, $from, $to, $message) {
    New-Item -ItemType Directory -Force data | Out-Null
    [pscustomobject]@{ ok = $ok; from = $from; to = $to; message = $message
                       at = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss") } |
        ConvertTo-Json | Set-Content -Path data\update_result.json -Encoding utf8
}
function RegisterTasks {
    if ($NoTasks) { return $true }
    & powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_tasks.ps1
    return ($LASTEXITCODE -eq 0)
}
function RestartServer {
    # Stop the running control server, then start the new code: the scheduled
    # task during its hours, otherwise one that closes itself when idle. An open
    # dashboard tab reloads itself on the new build.
    if ($NoRestart) { return }
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*tradingbot.run_control_server*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    $ny = [TimeZoneInfo]::ConvertTime((Get-Date), [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time"))
    $inHours = $ny.DayOfWeek -notin "Saturday", "Sunday" -and $ny.TimeOfDay -ge [timespan]"08:00" -and $ny.TimeOfDay -lt [timespan]"18:00"
    if ($inHours -and (Get-ScheduledTask -TaskName BotControlServer -ErrorAction SilentlyContinue)) {
        Start-ScheduledTask -TaskName BotControlServer
    } else {
        Start-Process -FilePath $py -ArgumentList "-m", "tradingbot.run_control_server", "--open", "--idle", "30" -WindowStyle Hidden
    }
}

$from = Version
Say "Updating from $from"

# 1. Nothing of yours in the way: no edited code files.
$dirty = git status --porcelain --untracked-files=no
if ($dirty) {
    Say "Edited files would be overwritten:`n$dirty"
    Result $false $from $from "Not updated: these files were edited on this PC: $(($dirty | ForEach-Object { $_.Substring(3) }) -join ', '). Undo the edits (or ask for help), then try again."
    RestartServer; exit 1
}

# 2. Get the release.
git fetch --tags --quiet origin
if ($LASTEXITCODE -ne 0) { Result $false $from $from "Not updated: couldn't reach GitHub. Check the internet connection and try again."; RestartServer; exit 1 }
if (-not $Version) {
    $Version = git tag --list "v*" | Where-Object { $_ -match '^v\d+\.\d+\.\d+$' } |
        Sort-Object { [version]$_.Substring(1) } | Select-Object -Last 1
}
if (-not $Version -or -not (git rev-parse -q --verify "refs/tags/$Version")) {
    Result $false $from $from "Not updated: release $Version not found."; RestartServer; exit 1
}
$old = (git rev-parse HEAD).Trim()

function Rollback($why) {
    Say "FAILED: $why - going back to $from"
    git merge --abort 2>$null | Out-Null
    git reset --hard --quiet $old
    & $py -m pip install --quiet -e ".[dev]" 2>&1 | Out-Null
    RegisterTasks | Out-Null
    Result $false $from $from "Update to $($Version.TrimStart('v')) failed ($why). Still on $from; nothing else changed. Details: logs\update_$(Get-Date -Format yyyy-MM-dd).log"
    RestartServer; exit 1
}

# The bots commit each day's reports and reviews to this copy, so it usually
# has commits of its own: merge the release in, keeping them. Where a file
# differs on both sides, the release's version wins (code is never edited
# here: step 1 refused edited files).
git merge --no-edit --quiet -X theirs -m "Update to $Version" $Version
if ($LASTEXITCODE -ne 0) { Rollback "the release couldn't be merged with this copy's own commits" }
# Reports and reviews stay on this PC only (no longer in git): stop tracking
# any this copy committed itself. The files are kept.
if (git ls-files reports analysis) {
    git rm -r --cached --quiet --ignore-unmatch reports analysis
    if ($LASTEXITCODE -eq 0) { git commit --quiet -m "Keep this PC's reports out of git" }
    if ($LASTEXITCODE -ne 0) { Rollback "couldn't stop tracking this PC's reports" }
}

# 3. Packages, a load check, the scheduled tasks.
& $py -m pip install --quiet -e ".[dev]"
if ($LASTEXITCODE -ne 0) { Rollback "installing the Python packages failed" }
& $py -c "import tradingbot.run_control_server, tradingbot.run_orb_bot, tradingbot.dashboard"
if ($LASTEXITCODE -ne 0) { Rollback "the new code didn't load" }
if (-not (RegisterTasks | Select-Object -Last 1)) { Rollback "re-registering the scheduled tasks failed" }

$to = Version
Say "Updated $from -> $to"
Result $true $from $to "Updated from $from to $to."
RestartServer
exit 0
