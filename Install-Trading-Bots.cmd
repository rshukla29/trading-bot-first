@echo off
rem One-click installer for the trading bots (docs/SETUP.md, step 4).
rem Double-click it. It installs Python and Git if they're missing, downloads
rem the bots into Documents\trading-bot and runs scripts\install.ps1.
rem Safe to run again: an existing download, .env and data are kept.
rem
rem The rest of this file is a PowerShell script; the lines below copy it to
rem a temporary file and run it, so nobody has to open PowerShell.
title Trading bots installer
set "TB_SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = [IO.File]::ReadAllText($env:TB_SELF); $p = Join-Path $env:TEMP 'trading-bots-setup.ps1'; [IO.File]::WriteAllText($p, $s.Substring($s.IndexOf('#' + '#PS-START')), (New-Object Text.UTF8Encoding $true)); & $p; exit $LASTEXITCODE"
exit /b %errorlevel%

##PS-START
# Everything below runs in PowerShell. ASCII only (PowerShell 5.1).
#
# For testing: TB_INSTALL_DIR (where to download), TB_REPO_URL (what to
# clone) and TB_SETUP_TEST=1 (no pop-ups, default name, -SkipTasks).

$RepoUrl = "https://github.com/rshukla29/trading-bot-first.git"
if ($env:TB_REPO_URL) { $RepoUrl = $env:TB_REPO_URL }
$Dir = Join-Path $HOME "Documents\trading-bot"
if ($env:TB_INSTALL_DIR) { $Dir = $env:TB_INSTALL_DIR }
$Test = ($env:TB_SETUP_TEST -eq "1")
$Guide = "https://github.com/rshukla29/trading-bot-first/blob/master/docs/SETUP.md"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName Microsoft.VisualBasic

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "   OK  $text" -ForegroundColor Green }
function Fail($text) {
    Write-Host "   PROBLEM  $text" -ForegroundColor Red
    Write-Host ""
    if (-not $Test) {
        [void][System.Windows.Forms.MessageBox]::Show(
            "$text`n`nFix that, then double-click the installer again. Running it twice is safe.",
            "Trading bots installer", "OK", "Error")
    }
    exit 1
}
function Ask($text, $buttons) {
    if ($Test) { return "OK" }
    return [System.Windows.Forms.MessageBox]::Show($text, "Trading bots installer", $buttons, "Information").ToString()
}
function Update-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
function Get-PythonVersion {
    # The Microsoft Store's "python" placeholder prints an error and exits 9009.
    try { $v = (& python -c "import sys; print('%d.%d' % sys.version_info[:2])") 2>$null } catch { return $null }
    if ($LASTEXITCODE -ne 0 -or -not $v) { return $null }
    try { return [version]("$v".Trim()) } catch { return $null }
}
function Test-Git {
    try { & git --version 2>$null | Out-Null } catch { return $false }
    return ($LASTEXITCODE -eq 0)
}
function Install-WithWinget($id, $what, $extra) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        if (-not $Test) { Start-Process "ms-windows-store://pdp/?productid=9NBLGGH4NNS1" }
        Fail "$what is missing, and Windows' App Installer (winget) isn't available to install it. Install 'App Installer' from the Microsoft Store (it just opened), or install $what yourself (docs/SETUP.md, step 3)."
    }
    Write-Host "   Installing $what (a few minutes)..."
    $wingetArgs = @("install", "--id", $id, "-e", "--source", "winget", "--silent",
              "--accept-package-agreements", "--accept-source-agreements") + $extra
    & winget @wingetArgs
    Update-Path
}

if ((Ask ("This installs the trading bots on this PC (paper trading only).`n`n" +
          "It installs Python and Git if they're missing, downloads the bots into`n$Dir`n" +
          "and sets up the scheduled tasks and the Trading dashboard shortcut.`n`n" +
          "It takes about 5-10 minutes. Windows may ask for permission once: click Yes.") "OKCancel") -ne "OK") {
    exit 1
}

Step "Python"
Update-Path
$py = Get-PythonVersion
if (-not $py -or $py -lt [version]"3.11") {
    Install-WithWinget "Python.Python.3.12" "Python 3.12" @("--scope", "user", "--override",
        "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=0 Include_test=0")
    $local = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312"
    if (Test-Path (Join-Path $local "python.exe")) { $env:Path = "$local;$local\Scripts;" + $env:Path }
    $py = Get-PythonVersion
    if (-not $py -or $py -lt [version]"3.11") {
        Fail "Python didn't install. Install Python 3.12 from python.org (tick 'Add python.exe to PATH'), then run this again."
    }
}
Ok "Python $py"

Step "Git"
if (-not (Test-Git)) {
    Install-WithWinget "Git.Git" "Git" @()
    $gitCmd = Join-Path $env:ProgramFiles "Git\cmd"
    if (Test-Path (Join-Path $gitCmd "git.exe")) { $env:Path = "$gitCmd;" + $env:Path }
    if (-not (Test-Git)) { Fail "Git didn't install. Install Git for Windows from git-scm.com, then run this again." }
}
Ok ((& git --version) 2>$null)

Step "Downloading the bots into $Dir"
if (Test-Path (Join-Path $Dir ".git")) {
    Ok "Already downloaded - kept as it is (updates come from the dashboard)"
} else {
    if ((Test-Path $Dir) -and (Get-ChildItem -Force $Dir | Select-Object -First 1)) {
        Fail "The folder $Dir already exists but isn't a download of the bots. Rename or move it, then run this again."
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $Dir -Parent) | Out-Null
    & git clone --quiet $RepoUrl $Dir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $Dir "scripts\install.ps1"))) {
        Fail "Downloading the bots failed. Check the internet connection."
    }
    Ok "Downloaded"
}

Step "Your name for the saved reports"
# The daily reports are saved as Git commits, which need a name. It stays on this PC.
$name = (& git -C $Dir config user.name) 2>$null
if ($name) {
    Ok "$name"
} else {
    $name = $env:USERNAME
    $email = ""
    if (-not $Test) {
        $n = [Microsoft.VisualBasic.Interaction]::InputBox(
            "Your name (the bots stamp it on the daily reports they save; it stays on this PC):",
            "Trading bots installer", $env:USERNAME)
        if ($n.Trim()) { $name = $n.Trim() }
        $email = [Microsoft.VisualBasic.Interaction]::InputBox(
            "Your email (optional, same use; leave it blank to skip):", "Trading bots installer", "").Trim()
    }
    if (-not $email) { $email = "$($env:USERNAME)@localhost" }
    & git -C $Dir config user.name $name
    & git -C $Dir config user.email $email
    Ok "$name"
}

Step "Setting up the bots (scripts\install.ps1)"
$installArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $Dir "scripts\install.ps1"))
if ($Test) { $installArgs += "-SkipTasks" }
& powershell @installArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }  # install.ps1 already showed the problem

if ((Ask ("The trading bots are installed.`n`n" +
          "Next: check the settings file and set up Trader Workstation (TWS) -`n" +
          "steps 6 to 8 of the setup guide. Open the guide now?") "YesNo") -eq "Yes") {
    Start-Process "$Guide#step-6-check-the-settings-file"
}
exit 0
