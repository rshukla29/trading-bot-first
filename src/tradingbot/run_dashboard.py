"""Build the dashboard (reports/dashboard.html) from the journal, reports,
reviews and logs. Read-only; no IBKR connection. See dashboard.py.

    python -m tradingbot.run_dashboard           # build
    python -m tradingbot.run_dashboard --open    # build and open it in the browser

--open (the "Trading dashboard" desktop shortcut) always opens the page
through the control server (run_control_server.py), so the Bot controls
tab's Start / Pause / Stop buttons work. If the server isn't running (e.g.
at the weekend), it starts one in the background that closes itself after
30 minutes with no dashboard tab open. Only if that fails does it open the
file, with the buttons offline.
"""
from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
import webbrowser
from datetime import date
from pathlib import Path

from tradingbot.config import settings
from tradingbot.dashboard import build

IDLE_MINUTES = 30


def control_server_url() -> str | None:
    url = f"http://127.0.0.1:{settings.control_port}/"
    try:  # /api/ping: doesn't count as a dashboard page checking in
        with urllib.request.urlopen(url + "api/ping", timeout=3) as r:
            return url if r.status == 200 else None
    except OSError:
        return None


def start_control_server() -> bool:
    """Start the control server in the background (no window); it rebuilds
    the dashboard and opens it itself. True once it answers."""
    Path("logs").mkdir(exist_ok=True)
    log = open(Path("logs") / f"control_server_{date.today():%Y-%m-%d}.log", "ab")
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if sys.platform == "win32" else 0
    subprocess.Popen([sys.executable, "-m", "tradingbot.run_control_server", "--open", "--now",
                      "--idle", str(IDLE_MINUTES)], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                     creationflags=flags, start_new_session=sys.platform != "win32")
    for _ in range(30):
        if control_server_url():
            return True
        time.sleep(0.5)
    return False


def main() -> None:
    if "--open" not in sys.argv[1:]:
        print(f"Dashboard: {build()}")
        return
    if control_server_url() is None and start_control_server():
        print("Control server started; it opens the dashboard")
        return
    path = build()
    print(f"Dashboard: {path}")
    webbrowser.open(control_server_url() or path.resolve().as_uri())


if __name__ == "__main__":
    main()
