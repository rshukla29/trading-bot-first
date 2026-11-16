"""A Windows notification (the balloon / toast in the corner), no extra
packages: a hidden PowerShell shows it through System.Windows.Forms."""
from __future__ import annotations

import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
    "$n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Information; "
    "$n.Visible = $true; $n.ShowBalloonTip(15000, $env:TB_NOTIFY_TITLE, $env:TB_NOTIFY_TEXT, 'Info'); "
    "Start-Sleep -Seconds 16; $n.Dispose()")


def windows_notify(title: str, text: str) -> bool:
    """Show it (in the background); False when not on Windows or it failed."""
    if sys.platform != "win32":
        return False
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _SCRIPT],
                         env={**os.environ, "TB_NOTIFY_TITLE": title, "TB_NOTIFY_TEXT": text},
                         creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError:
        logger.exception("Couldn't show the notification")
        return False
    return True
