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
from tradingbot.strategy.factory import build_strategy

logging.basicConfig(level=logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", default="3 Y", help="IBKR historical duration string")
    parser.add_argument("--equity", type=float, default=100_000.0)
    args = parser.parse_args()

    strategy = build_strategy(settings)
    warmup = strategy.min_history

    all_trades = []
    total_return = 0.0

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
                warmup=warmup,
            )
            closed = result.closed_trades
            all_trades.extend(closed)
            total_return += result.total_return_pct
            print(
                f"{symbol:<8}{len(closed):>8}"
                f"{result.win_rate * 100:>9.1f}%"
                f"{result.total_return_pct * 100:>9.1f}%"
                f"{result.max_drawdown_pct * 100:>9.1f}%"
            )

    print(f"\n{'TOTAL':<8}{len(all_trades):>8}{'':>10}{total_return * 100:>9.1f}%")

    print(f"\n{'Entry source':<24}{'Trades':>8}{'Win Rate':>10}{'Net P&L':>14}")
    by_source: dict[str, list] = {}
    for trade in all_trades:
        by_source.setdefault(trade.entry_source, []).append(trade)
    for source, trades in sorted(by_source.items()):
        wins = sum(1 for t in trades if (t.pnl or 0) > 0)
        net = sum(t.pnl or 0 for t in trades)
        print(f"{source:<24}{len(trades):>8}{wins / len(trades) * 100:>9.1f}%{net:>14,.0f}")


if __name__ == "__main__":
    main()
