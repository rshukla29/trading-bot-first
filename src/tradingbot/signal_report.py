"""CLI: compute today's strategy signals for the configured watchlist and
print them as JSON to stdout.

Used directly by run_ibkr_bot.py, and intended to be shelled out to by the
Robinhood side (see robinhood/AGENT_INSTRUCTIONS.md) so both brokers trade
off the exact same signal logic without a second implementation.

    python -m tradingbot.signal_report
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict

from tradingbot.config import settings
from tradingbot.ibkr.client import IBKRClient
from tradingbot.ibkr.data import fetch_daily_bars
from tradingbot.journal import Journal
from tradingbot.strategy.ma_crossover import MovingAverageCrossoverStrategy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    strategy = MovingAverageCrossoverStrategy(fast=settings.fast_ma, slow=settings.slow_ma)
    journal = Journal(settings.db_path)

    signals = []
    with IBKRClient(settings) as client:
        for symbol in settings.symbols:
            try:
                bars = fetch_daily_bars(client.ib, symbol, duration="1 Y")
            except Exception:
                logger.exception("Failed to fetch bars for %s", symbol)
                continue

            signal = strategy.generate_signal(symbol, bars)
            journal.log_signal(signal.symbol, signal.action, signal.price, signal.reason, source="signal_report")
            if signal.action != "HOLD":
                signals.append(asdict(signal) | {"timestamp": str(signal.timestamp)})

    json.dump(signals, sys.stdout, default=str, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
