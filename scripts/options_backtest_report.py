"""CLI: backtest the intraday opening-range-breakout options strategy.

Option prices are Black-Scholes simulations off real historical intraday
underlying bars (see src/tradingbot/options/pricing.py) — this account has
no historical options data access, confirmed via a live probe (empty bars
for both live and expired option contracts). Treat P&L as directional and
mechanism-level, not a substitute for real fills/spreads/liquidity.

    python scripts/options_backtest_report.py [--duration "20 D"]
"""
from __future__ import annotations

import argparse
import logging

from tradingbot.config import settings
from tradingbot.ibkr.client import IBKRClient
from tradingbot.ibkr.data import fetch_daily_bars
from tradingbot.options.backtest import run_options_backtest
from tradingbot.options.orb_strategy import OpeningRangeBreakoutStrategy

logging.basicConfig(level=logging.WARNING)


def fetch_intraday_bars(ib, symbol: str, bar_minutes: int, duration: str):
    from ib_async import Stock, util

    contract = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(contract)
    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting=f"{bar_minutes} mins",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,  # exchange-local tz-aware timestamps, so 15:55 means 15:55 ET
    )
    df = util.df(bars)
    if df is None or df.empty:
        raise ValueError(f"No intraday bars returned for {symbol}")
    df = df.set_index("date")
    return df[["open", "high", "low", "close", "volume"]]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", default="20 D", help="IBKR historical duration string")
    parser.add_argument("--equity", type=float, default=100_000.0)
    args = parser.parse_args()

    strategy = OpeningRangeBreakoutStrategy(
        opening_range_minutes=settings.options_opening_range_minutes,
        bar_minutes=settings.options_bar_minutes,
    )
    profit_target = settings.options_profit_target_pct if settings.options_profit_target_pct > 0 else None

    with IBKRClient(settings) as client:
        bars = fetch_intraday_bars(client.ib, settings.options_symbol, settings.options_bar_minutes, args.duration)
        print(f"{settings.options_symbol}: {len(bars)} bars, {bars.index[0]} .. {bars.index[-1]}")

        result = run_options_backtest(
            settings.options_symbol,
            bars,
            strategy,
            starting_equity=args.equity,
            risk_pct_per_trade=settings.options_risk_pct_per_trade,
            stop_loss_pct_of_premium=settings.options_stop_loss_pct_of_premium,
            profit_target_pct=profit_target,
            dte_days=settings.options_dte_days,
            otm_pct=settings.options_otm_pct,
            strike_increment=settings.options_strike_increment,
            vol_window=settings.options_vol_window,
            iv_premium_multiplier=settings.options_iv_premium_multiplier,
            rate=settings.options_rate,
            exit_on_range_reentry=settings.options_exit_on_range_reentry,
        )

    closed = result.closed_trades
    print(f"\nTrades: {len(closed)}   Win rate: {result.win_rate * 100:.1f}%")
    print(f"Total P&L: ${result.total_pnl:,.0f}   Return: {result.total_return_pct * 100:.1f}%")

    if closed:
        print(f"\n{'Right':<6}{'Entry':<20}{'Strike':>8}{'Entry$':>9}{'Exit$':>9}{'P&L':>10}{'Reason':<16}")
        for t in closed:
            print(
                f"{t.right:<6}{str(t.entry_time):<20}{t.strike:>8.1f}{t.entry_premium:>9.2f}"
                f"{t.exit_premium:>9.2f}{t.pnl:>10,.0f}  {t.exit_reason}"
            )

        by_reason: dict[str, list] = {}
        for t in closed:
            by_reason.setdefault(t.exit_reason, []).append(t)
        print(f"\n{'Exit reason':<16}{'Count':>7}{'Net P&L':>12}")
        for reason, trades in sorted(by_reason.items()):
            print(f"{reason:<16}{len(trades):>7}{sum(t.pnl for t in trades):>12,.0f}")


if __name__ == "__main__":
    main()
