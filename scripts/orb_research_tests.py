"""ORB variants from published practitioner rules, re-tested on SPY with
VIX-calibrated option pricing (2026-10-01 research follow-up).

    python scripts/orb_research_tests.py [--stress]
"""
from __future__ import annotations

import sys
from datetime import time

import numpy as np
import pandas as pd

from scripts.credit_orb_backtest import load, load_spy
from tradingbot.options.backtest import run_options_backtest
from tradingbot.options.orb_strategy import OpeningRangeBreakoutStrategy
from tradingbot.options.pricing import vix_vol_series


def max_dd(curve):
    peak, worst = -np.inf, 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        worst = min(worst, eq / peak - 1)
    return worst


def main() -> None:
    spy = load_spy()
    vol = vix_vol_series(spy.index, load("data/vix_5min.csv")["close"])
    stress = "--stress" in sys.argv
    s5, s15 = OpeningRangeBreakoutStrategy(5, 5), OpeningRangeBreakoutStrategy(15, 5)
    common = dict(symbol="SPY", bars=spy, starting_equity=100_000, risk_pct_per_trade=0.01,
                  stop_loss_pct_of_premium=0.5, profit_target_pct=None, dte_days=0, otm_pct=0.0,
                  strike_increment=1.0, vol_window=20, iv_premium_multiplier=1.2, rate=0.04,
                  exit_on_range_reentry=False, use_stop_loss=False, vwap_exit=False, slippage_per_share=0.02,
                  commission_per_contract=0.65, max_contracts=10, compound=False)
    ladder = dict(ladder_trigger_pct=0.5, ladder_step_pct=0.25, ladder_trail_pct=0.25)
    cafe = dict(profit_target_pct=1.0, use_stop_loss=True, stop_loss_pct_of_premium=0.5,
                eod_flatten_time=time(15, 30), max_trades_per_day=1)
    live = dict(strategy=s15, vwap_exit=True, max_reentries=2, **ladder)
    rec = dict(strategy=s15, last_entry_time=time(12, 0), **ladder)
    filt = dict(min_range_pct=0.002, require_vwap_side=True, confirm_bars=2)
    if "--fib" in sys.argv:
        best = dict(rec, **filt)
        V = {
            "current live (step-up + 3:55, <12:00, 3 filters)": best,
            "  + exit at 127.2% ext": dict(best, fib_target_ext=1.272),
            "  + exit at 161.8% ext": dict(best, fib_target_ext=1.618),
            "  + exit at 200% ext": dict(best, fib_target_ext=2.0),
            "  + exit at 261.8% ext": dict(best, fib_target_ext=2.618),
            "  + lock: hit 127.2% then back to range edge": dict(best, fib_lock_ext=1.272),
            "  + lock: hit 161.8% then back to range edge": dict(best, fib_lock_ext=1.618),
            "  fib 161.8% target instead of step-up": dict(best, ladder_trigger_pct=None, fib_target_ext=1.618),
            "  fib 261.8% target instead of step-up": dict(best, ladder_trigger_pct=None, fib_target_ext=2.618),
        }
    else:
      V = {
        "options.cafe: 5m range, +100%/-50%/3:30, 1/day, Mon-Wed-Fri": dict(strategy=s5, **cafe, trade_weekdays=(0, 2, 4)),
        "options.cafe rules, all weekdays": dict(strategy=s5, **cafe),
        "options.cafe exits, 15m range": dict(strategy=s15, **cafe),
        "live now (15m, VWAP exit, step-up, re-entry x2)": live,
        "live now + min range 0.2%": dict(live, min_range_pct=0.002),
        "live now + VWAP side": dict(live, require_vwap_side=True),
        "live now + 2-bar confirm": dict(live, confirm_bars=2),
        "live now + all 3 filters": dict(live, **filt),
        "held change (step-up + 3:55, <12:00)": rec,
        "held change + all 3 filters": dict(rec, **filt),
        "held change + 5m range": dict(rec, strategy=s5),
        "held change + 5m range + all 3 filters": dict(rec, strategy=s5, **filt),
    }
    rows = []
    for name, kw in V.items():
        runs = [("base", {}, vol)]
        if stress:
            runs += [("slip .05", {"slippage_per_share": 0.05}, vol), ("IV +25%", {}, vol * 1.25)]
        for label, extra, v in runs:
            r = run_options_backtest(**{**common, **kw, **extra}, vol_override=v)
            pn = [t.pnl for t in r.closed_trades]
            w = [p for p in pn if p > 0]
            gl = -sum(p for p in pn if p <= 0)
            rows.append(dict(variant=name, stress=label, trades=len(pn), win=round(100 * len(w) / max(len(pn), 1), 1),
                             net=round(sum(pn)), pf=round(sum(w) / gl, 2) if gl else np.inf,
                             dd=round(100 * max_dd(r.equity_curve), 1)))
            print(rows[-1], flush=True)
    pd.set_option("display.width", 250)
    print("\n" + pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
