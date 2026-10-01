"""Backtest ORB credit spreads on SPY with VIX-calibrated option pricing.

    python scripts/credit_orb_backtest.py

Needs data/spy_5min.csv (from scripts/orb_exit_comparison.py) and
data/vix_5min.csv (VIX 5-min history). Prices are Black-Scholes at a
VIX-calibrated implied vol (options/pricing.vix_vol_series) with a flat
smile — no put skew — so treat results as indicative.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from datetime import time
from pathlib import Path

import pandas as pd

from tradingbot.options.credit_orb import CreditOrbConfig, backtest_credit_orb, summarize
from tradingbot.options.pricing import vix_vol_series

ET = "America/New_York"


def load(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(ET)
    return df


def load_spy() -> pd.DataFrame:
    frames = [load("data/spy_5min.csv")]
    extra = Path("analysis/2026-10-01_orb_loss_analysis/data/spy_5min_2026-08-03_to_2026-10-01.csv")
    if extra.exists():
        frames.append(load(str(extra)))
    spy = pd.concat(frames).sort_index()
    return spy[~spy.index.duplicated(keep="last")][["open", "high", "low", "close", "volume"]]


def main() -> None:
    spy = load_spy()
    vix = load("data/vix_5min.csv")["close"]
    vol = vix_vol_series(spy.index, vix)
    stress = len(sys.argv) > 1 and sys.argv[1] == "--stress"
    print(f"SPY {spy.index[0].date()} .. {spy.index[-1].date()}, {len(set(spy.index.date))} sessions\n")

    base = CreditOrbConfig()
    variants = {
        "OA/Quantish: 60m range, 10:30-12:00, short at range edge, $2 wide": base,
        "  + skip Wed & Fri (Quantish refinement)": replace(base, skip_weekdays=(2, 4)),
        "  short strike at range midpoint (OA optimized)": replace(base, anchor="mid"),
        "  $1 wide": replace(base, width=1.0),
        "  $3 wide": replace(base, width=3.0),
        "  take profit at 50% of credit": replace(base, profit_take_pct=0.5),
        "  no min range filter": replace(base, min_range_pct=0.0),
        "30m range, 10:00-12:00": replace(base, range_minutes=30),
        "15m range, 9:45-12:00": replace(base, range_minutes=15),
        "60m range, entries until 14:00": replace(base, entry_end=time(14, 0)),
    }
    rows, halves = [], {}
    for name, cfg in variants.items():
        runs = [("base", cfg, vol)]
        if stress:
            runs += [("slip .05/leg", replace(cfg, slippage_per_leg=0.05), vol), ("IV +25%", cfg, vol * 1.25),
                     ("IV -20%", cfg, vol * 0.8)]
        for label, c, v in runs:
            trades = backtest_credit_orb(spy, v, c)
            rows.append(dict(variant=name, stress=label, **summarize(trades)))
            if label == "base":
                per = {}
                for t in trades:
                    k = f"{t.exit_ts.year}-H{1 if t.exit_ts.month <= 6 else 2}"
                    per[k] = per.get(k, 0) + t.pnl
                halves[name.strip()[:24]] = per
    pd.set_option("display.width", 250)
    print(pd.DataFrame(rows).to_string(index=False))
    print("\nNet P&L by half-year:")
    print(pd.DataFrame(halves).sort_index().round(0).fillna(0).astype(int).to_string())


if __name__ == "__main__":
    main()
