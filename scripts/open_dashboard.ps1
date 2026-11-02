# Rebuild the dashboard from the latest data and open it in the browser.
# Right-click > "Run with PowerShell". Read-only: it never talks to IBKR.
Set-Location (Split-Path $PSScriptRoot -Parent)  # the repo folder, wherever it was cloned
& ".\.venv\Scripts\python.exe" -m tradingbot.run_dashboard --open
