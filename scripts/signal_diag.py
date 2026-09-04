"""CLI: count signals by source across the watchlist, and show which filter
is blocking otherwise-valid crossovers.

Useful for spotting an over-tight filter: if one filter accounts for most
blocks, it's probably rejecting valid signals rather than bad ones.

    python scripts/signal_diag.py [--duration "3 Y"]
"""
from __future__ import annotations

import argparse
import collections
import logging

from tradingbot.config import settings
from tradingbot.ibkr.client import IBKRClient
from tradingbot.ibkr.data import fetch_daily_bars
from tradingbot.strategy.factory import build_strategy

logging.basicConfig(level=logging.WARNING)

FILTER_LABELS = ("trend filter", "volume", "RSI", "VWAP")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", default="3 Y", help="IBKR historical duration string")
    args = parser.parse_args()

    strategy = build_strategy(settings)
    buys: collections.Counter = collections.Counter()
    exits: collections.Counter = collections.Counter()
    blocks: collections.Counter = collections.Counter()

    with IBKRClient(settings) as client:
        for symbol in settings.symbols:
            bars = fetch_daily_bars(client.ib, symbol, duration=args.duration)
            for i in range(strategy.min_history, len(bars)):
                signal = strategy.generate_signal(symbol, bars.iloc[: i + 1])
                if signal.action == "BUY":
                    buys["consolidation retest" if "consolidation" in signal.reason else "MA crossover"] += 1
                elif signal.action == "EXIT":
                    exits["death cross" if "death" in signal.reason else "RSI exhaustion"] += 1
                elif "blocked" in signal.reason:
                    detail = signal.reason.split("blocked: ")[1]
                    for label in FILTER_LABELS:
                        if label.lower() in detail.lower():
                            blocks[label] += 1
                            break

    for title, counter in (("BUY signals", buys), ("EXIT signals", exits), ("Entries blocked, by filter", blocks)):
        print(f"\n{title}:")
        for name, count in counter.most_common():
            print(f"  {name:<24}{count:>6}")


if __name__ == "__main__":
    main()
