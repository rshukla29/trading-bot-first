# Creates (or replaces) the bots' Task Scheduler tasks from scripts\schedule.json.
# Run by install.ps1; safe to run again on its own at any time:
#     powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1
#
# Times in schedule.json are New York time. Each is converted to this PC's
# clock and stored WITHOUT a UTC offset, so the task follows the clock
# through daylight-saving changes. (New-ScheduledTaskTrigger stores today's
# offset, e.g. 09:25-04:00, which pins the task to UTC: after the 2026-11-01
# change six bots ran an hour early.) The control server's Bot controls tab
# warns if a task's next run ever drifts from schedule.json again.

$repo = Split-Path $PSScriptRoot -Parent
$cfg = Get-Content (Join-Path $PSScriptRoot "schedule.json") -Raw | ConvertFrom-Json
$ny = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$days = [string[]]$cfg.days

function Ok($text) { Write-Host "   OK  $text" -ForegroundColor Green }

function LocalWallClock([string]$nyTime) {
    $nyClock = [datetime]::SpecifyKind([datetime]::Today.Add([timespan]$nyTime), "Unspecified")
    [TimeZoneInfo]::ConvertTime($nyClock, $ny, [TimeZoneInfo]::Local)
}

function WeeklyTrigger([datetime]$local) {
    $t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $local
    $t.StartBoundary = $local.ToString("yyyy-MM-ddTHH:mm:ss")  # no offset: follows this PC's clock
    $t
}

$botSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 72) -StartWhenAvailable
# The control server stops itself at 6pm; restart it if it crashes, never run two.
$serverSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 11) -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

foreach ($task in $cfg.tasks) {
    $path = Join-Path $repo "scripts\$($task.script)"
    if (-not (Test-Path $path)) { continue }
    $local = LocalWallClock $task.ny
    $hidden = if ($task.at_logon) { "-WindowStyle Hidden " } else { "" }
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass $hidden-File `"$path`""
    $triggers = @(WeeklyTrigger $local)
    $settings = $botSettings
    if ($task.at_logon) {  # the control server: also at logon (it only runs Mon-Fri 8am-6pm)
        $triggers += New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = $serverSettings
    }
    Register-ScheduledTask -TaskName $task.name -Action $action -Trigger $triggers -Settings $settings -Force | Out-Null
    Ok ("{0,-22} {1} New York = {2} on this PC" -f $task.name, $task.ny, $local.ToString("HH:mm"))
}

if ([TimeZoneInfo]::Local.Id -ne $ny.Id) {
    Write-Host "   This PC isn't on New York time. If your clock changes for daylight saving on"
    Write-Host "   different dates than the US, run this again after each change (the Bot controls"
    Write-Host "   tab warns when a task's time is off)."
}
