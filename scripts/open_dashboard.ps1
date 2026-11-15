# Rebuild the dashboard from the latest data and open it in the browser.
# Right-click > "Run with PowerShell". Never talks to IBKR. If the control
# server isn't running (weekends, evenings) it starts one in the background,
# so the Start / Pause / Stop buttons work; it closes itself 30 minutes
# after the last dashboard tab is closed.
Set-Location (Split-Path $PSScriptRoot -Parent)  # the repo folder, wherever it was cloned
& ".\.venv\Scripts\python.exe" -m tradingbot.run_dashboard --open
