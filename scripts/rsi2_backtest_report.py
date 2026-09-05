"""CLI: backtest the Connors RSI(2) mean-reversion strategy across the
configured watchlist.

    python scripts/rsi2_backtest_report.py [--duration "3 Y"]
"""
from __future__ import annotations

import argparse
import logging

from tradingbot.backtest import run_backtest
from tradingbot.config import settings
from tradingbot.ibkr.client import IBKRClient
from tradingbot.ibkr.data import fetch_daily_bars
from tradingbot.strategy.rsi2_mean_reversion import RSI2MeanReversionStrategy

logging.basicConfig(level=logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default=",".join(settings.symbols), help="comma-separated symbol list")
    parser.add_argument("--duration", default="3 Y", help="IBKR historical duration string")
    parser.add_argument("--equity", type=float, default=100_000.0)
    parser.add_argument("--trend-ma", type=int, default=200)
    parser.add_argument("--buy-threshold", type=float, default=5.0)
    parser.add_argument("--sell-threshold", type=float, default=65.0)
    parser.add_argument("--fib-lookback", type=int, default=0, help="0 disables the fib retracement filter")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    strategy = RSI2MeanReversionStrategy(
        trend_ma=args.trend_ma,
        buy_threshold=args.buy_threshold,
        sell_threshold=args.sell_threshold,
        fib_lookback=args.fib_lookback or None,
    )
    warmup = strategy.min_history

    all_trades = []
    print(f"{'Symbol':<8}{'Trades':>8}{'Win Rate':>10}{'Return':>10}{'MaxDD':>10}")
    with IBKRClient(settings) as client:
        for symbol in symbols:
            bars = fetch_daily_bars(client.ib, symbol, duration=args.duration)
            if len(bars) < warmup:
                print(f"{symbol:<8}  insufficient history ({len(bars)} bars, need {warmup})")
                continue
            result = run_backtest(
                symbol, bars, strategy,
                starting_equity=args.equity,
                risk_pct=settings.risk_pct_per_trade,
                stop_loss_pct=settings.stop_loss_pct,
                warmup=warmup,
            )
            closed = result.closed_trades
            all_trades.extend(closed)
            print(
                f"{symbol:<8}{len(closed):>8}"
                f"{result.win_rate * 100:>9.1f}%"
                f"{result.total_return_pct * 100:>9.1f}%"
                f"{result.max_drawdown_pct * 100:>9.1f}%"
            )

    if all_trades:
        wins = sum(1 for t in all_trades if (t.pnl or 0) > 0)
        total_pnl = sum(t.pnl or 0 for t in all_trades)
        print(f"\nCOMBINED: {len(all_trades)} trades, win rate {wins / len(all_trades) * 100:.1f}%, "
              f"total P&L ${total_pnl:,.0f}")

        stop_loss_exits = [t for t in all_trades if t.exit_reason == "stop_loss"]
        rsi_exits = [t for t in all_trades if t.exit_reason and "RSI" in t.exit_reason]
        print(f"  RSI(2) overbought exits: {len(rsi_exits)}, net P&L ${sum(t.pnl or 0 for t in rsi_exits):,.0f}")
        print(f"  stop-loss exits: {len(stop_loss_exits)}, net P&L ${sum(t.pnl or 0 for t in stop_loss_exits):,.0f}")


if __name__ == "__main__":
    main()
