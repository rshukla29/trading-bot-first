"""Build the dashboard (reports/dashboard.html) from the journal, reports,
reviews and logs. Read-only; no IBKR connection. See dashboard.py.

    python -m tradingbot.run_dashboard           # build
    python -m tradingbot.run_dashboard --open    # build and open it in the browser

--open goes through the control server (run_control_server.py) when it's
running, so the Controls tab's buttons work; otherwise it opens the file.
"""
from __future__ import annotations

import sys
import urllib.request
import webbrowser

from tradingbot.config import settings
from tradingbot.dashboard import build


def control_server_url() -> str | None:
    url = f"http://127.0.0.1:{settings.control_port}/"
    try:
        with urllib.request.urlopen(url + "api/state", timeout=3) as r:
            return url if r.status == 200 else None
    except OSError:
        return None


def main() -> None:
    path = build()
    print(f"Dashboard: {path}")
    if "--open" in sys.argv[1:]:
        webbrowser.open(control_server_url() or path.resolve().as_uri())


if __name__ == "__main__":
    main()
