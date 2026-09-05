"""CLI: backtest the 16-delta iron condor credit-spread strategy across
one or more symbols (default SPY, QQQ, IWM), daily bars.

Strikes/premiums are Black-Scholes simulations off real historical daily
bars — this account has no historical options data access (see the ORB
backtest script for the probe that confirmed this). Treat results as
mechanism-level validation, not a substitute for real fills/spreads.

    python scripts/iron_condor_backtest_report.py --symbols SPY,QQQ,IWM --duration "3 Y"
"""
from __future__ import annotations

import argparse
import logging

from tradingbot.config import settings
from tradingbot.ibkr.client import IBKRClient
from tradingbot.ibkr.data import fetch_daily_bars
from tradingbot.options.iron_condor import IronCondorConfig, run_iron_condor_backtest

logging.basicConfig(level=logging.WARNING)

MAX_INSTRUMENTS = 100


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="SPY,QQQ,IWM", help="comma-separated symbol list")
    parser.add_argument("--duration", default="3 Y", help="IBKR historical duration string")
    parser.add_argument("--equity", type=float, default=100_000.0)
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if len(symbols) > MAX_INSTRUMENTS:
        raise SystemExit(f"Refusing to request {len(symbols)} instruments from IBKR (cap is {MAX_INSTRUMENTS}).")

    config = IronCondorConfig(
        target_delta=settings.credit_spread_target_delta,
        wing_width=settings.credit_spread_wing_width,
        dte_days=settings.credit_spread_dte_days,
        profit_target_pct=settings.credit_spread_profit_target_pct,
        stop_loss_multiple=settings.credit_spread_stop_loss_multiple,
        dte_forced_close=settings.credit_spread_dte_forced_close,
        entry_interval_days=settings.credit_spread_entry_interval_days,
        max_concurrent_positions=settings.credit_spread_max_concurrent,
        vol_window=settings.credit_spread_vol_window,
        iv_premium_multiplier=settings.credit_spread_iv_premium_multiplier,
        rate=settings.credit_spread_rate,
        min_vol_percentile=settings.credit_spread_min_vol_percentile or None,
        vol_percentile_lookback=settings.credit_spread_vol_percentile_lookback,
    )
    warmup = config.vol_window + 2
    if config.min_vol_percentile is not None:
        warmup = max(warmup, config.vol_percentile_lookback + config.vol_window + 2)

    all_closed = []
    per_symbol_pnl = {}

    with IBKRClient(settings) as client:
        for symbol in symbols:
            bars = fetch_daily_bars(client.ib, symbol, duration=args.duration)
            result = run_iron_condor_backtest(
                symbol, bars, config,
                starting_equity=args.equity,
                risk_pct_per_trade=settings.credit_spread_risk_pct_per_trade,
                warmup=warmup,
            )
            closed = result.closed_trades
            all_closed.extend(closed)
            per_symbol_pnl[symbol] = result.total_pnl

            print(f"\n{'=' * 60}\n{symbol}: {len(bars)} bars, {bars.index[0].date()} .. {bars.index[-1].date()}")
            print(f"Trades: {len(closed)}   Win rate: {result.win_rate * 100:.1f}%")
            print(f"Total P&L: ${result.total_pnl:,.0f}   Return: {result.total_return_pct * 100:.1f}%")
            if closed:
                by_reason: dict[str, list] = {}
                for t in closed:
                    by_reason.setdefault(t.exit_reason, []).append(t)
                print(f"{'Exit reason':<18}{'Count':>7}{'Net P&L':>12}")
                for reason, trades in sorted(by_reason.items()):
                    print(f"{reason:<18}{len(trades):>7}{sum(t.pnl or 0 for t in trades):>12,.0f}")

    if len(symbols) > 1 and all_closed:
        print(f"\n{'=' * 60}\nCOMBINED ACROSS {len(symbols)} SYMBOLS")
        wins = sum(1 for t in all_closed if (t.pnl or 0) > 0)
        total_pnl = sum(t.pnl or 0 for t in all_closed)
        print(f"Trades: {len(all_closed)}   Win rate: {wins / len(all_closed) * 100:.1f}%   Total P&L: ${total_pnl:,.0f}")
        print(f"\n{'Symbol':<8}{'Net P&L':>12}")
        for symbol, pnl in per_symbol_pnl.items():
            print(f"{symbol:<8}{pnl:>12,.0f}")

        by_reason = {}
        for t in all_closed:
            by_reason.setdefault(t.exit_reason, []).append(t)
        print(f"\n{'Exit reason':<18}{'Count':>7}{'Net P&L':>12}")
        for reason, trades in sorted(by_reason.items()):
            print(f"{reason:<18}{len(trades):>7}{sum(t.pnl or 0 for t in trades):>12,.0f}")


if __name__ == "__main__":
    main()
