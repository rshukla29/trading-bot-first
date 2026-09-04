"""CLI: run the MA-crossover strategy's backtest over each watchlist symbol
and print a summary report.

    python scripts/backtest_report.py [--duration "2 Y"]
"""
from __future__ import annotations

import argparse
import logging

from tradingbot.backtest import run_backtest
from tradingbot.config import settings
from tradingbot.ibkr.client import IBKRClient
from tradingbot.ibkr.data import fetch_daily_bars
from tradingbot.strategy.ma_crossover import MovingAverageCrossoverStrategy

logging.basicConfig(level=logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", default="2 Y", help="IBKR historical duration string")
    parser.add_argument("--equity", type=float, default=100_000.0)
    args = parser.parse_args()

    strategy = MovingAverageCrossoverStrategy(fast=settings.fast_ma, slow=settings.slow_ma)

    print(f"{'Symbol':<8}{'Trades':>8}{'Win Rate':>10}{'Return':>10}{'MaxDD':>10}")
    with IBKRClient(settings) as client:
        for symbol in settings.symbols:
            bars = fetch_daily_bars(client.ib, symbol, duration=args.duration)
            result = run_backtest(
                symbol,
                bars,
                strategy,
                starting_equity=args.equity,
                risk_pct=settings.risk_pct_per_trade,
                stop_loss_pct=settings.stop_loss_pct,
                warmup=settings.slow_ma + 1,
            )
            closed = result.closed_trades
            print(
                f"{symbol:<8}{len(closed):>8}"
                f"{result.win_rate * 100:>9.1f}%"
                f"{result.total_return_pct * 100:>9.1f}%"
                f"{result.max_drawdown_pct * 100:>9.1f}%"
            )


if __name__ == "__main__":
    main()
