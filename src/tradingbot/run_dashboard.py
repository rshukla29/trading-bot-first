"""Build the dashboard (reports/dashboard.html) from the journal, reports,
reviews and logs. Read-only; no IBKR connection. See dashboard.py.

    python -m tradingbot.run_dashboard           # build
    python -m tradingbot.run_dashboard --open    # build and open it in the browser
"""
from __future__ import annotations

import sys
import webbrowser

from tradingbot.dashboard import build


def main() -> None:
    path = build()
    print(f"Dashboard: {path}")
    if "--open" in sys.argv[1:]:
        webbrowser.open(path.resolve().as_uri())


if __name__ == "__main__":
    main()
