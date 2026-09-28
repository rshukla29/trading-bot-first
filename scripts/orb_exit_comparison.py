"""Compare ORB exit rules on the same SPY history — in particular a session
VWAP trailing-stop exit (the best-supported VWAP use in the research:
Zarattini/Aziz/Barbon's SPY intraday momentum paper and Maróy's follow-up
found VWAP-based exits strongest) against the current live exits.

Same entries in every variant (15-min opening range breakout, 0DTE ATM);
only the exits differ. Same caveats as options_backtest_report.py: option
prices are Black-Scholes simulations off real SPY 5-min bars (no historical
options data on this account), so treat results as relative comparisons
between variants, not as expected live P&L.

    python scripts/orb_exit_comparison.py --years 4
    python scripts/orb_exit_comparison.py --years 4 --slippage 0.03   # cost sensitivity

Bars are cached in data/<symbol>_5min.csv (gitignored); --refresh refetches.
"""
from __future__ import annotations

import argparse
import logging
import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from ib_async import IB, Stock, util

from tradingbot.config import settings
from tradingbot.options.backtest import run_options_backtest
from tradingbot.options.orb_strategy import OpeningRangeBreakoutStrategy

logging.basicConfig(level=logging.WARNING)

RESEARCH_CLIENT_ID = 94  # read-only historical data; distinct from every bot's ID
CHUNK = "6 M"  # larger single 5-min requests time out on this account


def fetch_history(symbol: str, years: int, refresh: bool) -> pd.DataFrame:
    cache = Path(f"data/{symbol.lower()}_5min.csv")
    if cache.exists() and not refresh:
        df = pd.read_csv(cache, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True).tz_convert("America/New_York")
        print(f"Loaded {len(df)} cached bars from {cache}")
        return df

    ib = IB()
    ib.connect(settings.ibkr_host, settings.ibkr_port, clientId=RESEARCH_CLIENT_ID, readonly=True)
    try:
        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)
        chunks, end = [], ""
        for n in range(years * 2):
            bars = ib.reqHistoricalData(
                contract, endDateTime=end, durationStr=CHUNK, barSizeSetting="5 mins",
                whatToShow="TRADES", useRTH=True, formatDate=2, timeout=300,
            )
            df = util.df(bars)
            if df is None or df.empty:
                print(f"  chunk {n + 1}: no data, stopping")
                break
            df = df.set_index("date")[["open", "high", "low", "close", "volume"]]
            df.index = pd.to_datetime(df.index, utc=True).tz_convert("America/New_York")
            chunks.append(df)
            print(f"  chunk {n + 1}: {len(df)} bars {df.index[0].date()} .. {df.index[-1].date()}")
            end = df.index[0].tz_convert("UTC").strftime("%Y%m%d-%H:%M:%S")
    finally:
        ib.disconnect()

    df = pd.concat(chunks).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    cache.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache)
    return df


@dataclass(frozen=True)
class Variant:
    name: str
    description: str
    kwargs: dict


def variants() -> list[Variant]:
    # The pre-2026-09-28 ORB exits, pinned here rather than read from
    # settings: live settings now have these switched off (VWAP-only).
    stop = settings.options_stop_loss_pct_of_premium
    pt = 0.50
    step = settings.step_stop_pct
    return [
        Variant("original", f"-{stop:.0%} stop, +{pt:.0%} target, range re-entry",
                dict(use_stop_loss=True, profit_target_pct=pt, exit_on_range_reentry=True)),
        Variant("live_now", "original + 10% step stop (what runs from Monday)",
                dict(use_stop_loss=True, profit_target_pct=pt, exit_on_range_reentry=True, step_stop_pct=step)),
        Variant("vwap_only", "VWAP trail only (+ EOD)",
                dict(use_stop_loss=False, profit_target_pct=None, exit_on_range_reentry=False, vwap_exit=True)),
        Variant("vwap_stop", f"VWAP trail + -{stop:.0%} stop",
                dict(use_stop_loss=True, profit_target_pct=None, exit_on_range_reentry=False, vwap_exit=True)),
        Variant("vwap_stop_step", f"VWAP trail + -{stop:.0%} stop + 10% step stop",
                dict(use_stop_loss=True, profit_target_pct=None, exit_on_range_reentry=False, vwap_exit=True,
                     step_stop_pct=step)),
        Variant("vwap_for_range", f"original, VWAP replacing range re-entry",
                dict(use_stop_loss=True, profit_target_pct=pt, exit_on_range_reentry=False, vwap_exit=True)),
    ]


def profit_booking_variants() -> list[Variant]:
    """VWAP-only exit plus one profit-booking rule each, to find a way to
    keep some gains on trades that reverse without capping the big winners
    the edge depends on."""
    base = dict(use_stop_loss=False, profit_target_pct=None, exit_on_range_reentry=False, vwap_exit=True)
    return [
        Variant("vwap_only", "VWAP trail only (baseline)", base),
        Variant("be_after_50", "+ breakeven lock once up 50%", {**base, "lock_trigger_pct": 0.5}),
        Variant("be_after_100", "+ breakeven lock once up 100%", {**base, "lock_trigger_pct": 1.0}),
        Variant("keep50_after_50", "+ keep half the peak gain once up 50%",
                {**base, "lock_trigger_pct": 0.5, "lock_keep_pct": 0.5}),
        Variant("keep50_after_100", "+ keep half the peak gain once up 100%",
                {**base, "lock_trigger_pct": 1.0, "lock_keep_pct": 0.5}),
        Variant("half_at_50", "+ sell half at +50%, rest rides", {**base, "partial_trigger_pct": 0.5}),
        Variant("half_at_100", "+ sell half at +100%, rest rides", {**base, "partial_trigger_pct": 1.0}),
        Variant("half_at_50_be", "+ sell half at +50%, breakeven on the rest",
                {**base, "partial_trigger_pct": 0.5, "lock_trigger_pct": 0.5}),
    ]


def max_drawdown(curve: list[tuple[pd.Timestamp, float]]) -> float:
    peak, worst = -math.inf, 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        worst = min(worst, eq / peak - 1)
    return worst


def summarize(name: str, result) -> dict:
    closed = result.closed_trades
    pnls = [t.pnl for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_loss = -sum(losses)
    return {
        "variant": name,
        "trades": len(closed),
        "win%": round(100 * len(wins) / len(closed), 1) if closed else 0.0,
        "net_pnl": round(sum(pnls)),
        "return%": round(result.total_return_pct * 100, 1),
        "profit_factor": round(sum(wins) / gross_loss, 2) if gross_loss else float("inf"),
        "avg_win": round(sum(wins) / len(wins)) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses)) if losses else 0,
        "max_dd%": round(max_drawdown(result.equity_curve) * 100, 1),
    }


def pnl_by_half_year(results: dict) -> pd.DataFrame:
    rows = {}
    for name, result in results.items():
        per = {}
        for t in result.closed_trades:
            key = f"{t.exit_time.year}-H{1 if t.exit_time.month <= 6 else 2}"
            per[key] = per.get(key, 0.0) + t.pnl
        rows[name] = per
    return pd.DataFrame(rows).sort_index().round(0).fillna(0).astype(int)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=settings.options_symbol)
    parser.add_argument("--years", type=int, default=4)
    parser.add_argument("--equity", type=float, default=100_000.0)
    parser.add_argument("--slippage", type=float, default=0.02, help="$/share per side (half the bid-ask + slip)")
    parser.add_argument("--commission", type=float, default=0.65, help="$/contract per side")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--profit-booking", action="store_true",
                        help="compare profit-booking rules on top of the VWAP exit instead")
    args = parser.parse_args()

    bars = fetch_history(args.symbol, args.years, args.refresh)
    print(f"{args.symbol}: {len(bars)} bars, {bars.index[0].date()} .. {bars.index[-1].date()}, "
          f"{len(set(bars.index.date))} sessions")
    print(f"Costs: ${args.slippage:.2f}/share slippage per side, ${args.commission:.2f}/contract commission per side\n")

    strategy = OpeningRangeBreakoutStrategy(
        opening_range_minutes=settings.options_opening_range_minutes, bar_minutes=settings.options_bar_minutes,
    )
    common = dict(
        symbol=args.symbol, bars=bars, strategy=strategy, starting_equity=args.equity,
        risk_pct_per_trade=settings.options_risk_pct_per_trade,
        stop_loss_pct_of_premium=settings.options_stop_loss_pct_of_premium,
        dte_days=settings.options_dte_days, otm_pct=settings.options_otm_pct,
        strike_increment=settings.options_strike_increment, vol_window=settings.options_vol_window,
        iv_premium_multiplier=settings.options_iv_premium_multiplier, rate=settings.options_rate,
        step_stop_buffer_pct=settings.step_stop_buffer_pct,
        slippage_per_share=args.slippage, commission_per_contract=args.commission,
        # Same 10-contract cap as live, no compounding: compares exits per
        # trade instead of rewarding whichever variant snowballs first.
        max_contracts=settings.options_max_contracts_per_trade, compound=False,
    )

    results, rows = {}, []
    for v in (profit_booking_variants() if args.profit_booking else variants()):
        result = run_options_backtest(**common, **v.kwargs)
        results[v.name] = result
        rows.append(summarize(v.name, result))
        print(f"  {v.name:<16} {v.description}")

    print("\n=== Summary ===")
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n=== Net P&L by half-year (does the edge hold across regimes?) ===")
    print(pnl_by_half_year(results).to_string())

    print("\n=== Exit reasons ===")
    for name, result in results.items():
        counts = pd.Series([t.exit_reason for t in result.closed_trades]).value_counts().to_dict()
        print(f"  {name:<16} {counts}")


if __name__ == "__main__":
    main()
