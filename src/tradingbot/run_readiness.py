"""Run the readiness check (readiness.py) and save data/readiness.json.

    python -m tradingbot.run_readiness            # print the results
    python -m tradingbot.run_readiness --notify   # also a Windows notification if anything fails

Started by the control server (8am, and Run checks now on the Bot health
tab). Never places orders. Exit code 1 if anything failed.
"""
from __future__ import annotations

import logging
import sys

from tradingbot import readiness

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ICON = {"pass": "OK  ", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}


def main() -> int:
    data = readiness.run(notify="--notify" in sys.argv[1:])
    for c in data["checks"]:
        print(f"{ICON[c['status']]}  {c['name']}: {c['detail']}")
        if c["fix"]:
            print(f"      Fix: {c['fix']}")
    s = readiness.summary(data["checks"])
    print(f"\n{'Ready to trade' if s['ready'] else 'Not ready'}: {s['pass']} OK, {s['warn']} warnings, "
          f"{s['fail']} failed, {s['skip']} not checked")
    return 0 if s["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
