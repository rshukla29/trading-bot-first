"""ORB (SPY 0DTE): would a time stop or a wide premium stop cut the
full-premium losses? (2026-11-03, after the live losers of Oct 2, Oct 7
and Oct 29 rode to $0.01 / -37%.)

Every variant is the live rule set (15-min range, noon cutoff, the three
entry filters, step-up stop, 200% Fibonacci target, 3:55 flatten) plus one
loss cut:
  - time stop at T: at or after T, sell while the option is below its entry
    price (or below +X%, where named);
  - premium stop: sell at -X% of the premium paid;
  - both.
Same engine and pricing as scripts/orb_research_tests.py (VIX-calibrated
Black-Scholes, $0.02/share slippage, $0.65/contract, 10 contracts max, 1%
risk, no compounding). Walk-forward: the variant is chosen on the first 60%
of sessions (by profit factor, at least 100 trades) and judged only on the
last 40%. Stress on the test days: options 25% pricier, $0.05 slippage.

    python scripts/orb_loss_cut_test.py
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from datetime import time

import numpy as np
import pandas as pd

from scripts.credit_orb_backtest import load, load_spy
from tradingbot.options.backtest import run_options_backtest
from tradingbot.options.orb_strategy import OpeningRangeBreakoutStrategy
from tradingbot.options.pricing import vix_vol_series

TRAIN_FRAC, MIN_TRADES = 0.6, 100
LIVE = dict(symbol="SPY", starting_equity=100_000, risk_pct_per_trade=0.01, stop_loss_pct_of_premium=0.5,
            profit_target_pct=None, dte_days=0, otm_pct=0.0, strike_increment=1.0, vol_window=20,
            iv_premium_multiplier=1.2, rate=0.04, exit_on_range_reentry=False, use_stop_loss=False,
            vwap_exit=False, slippage_per_share=0.02, commission_per_contract=0.65, max_contracts=10,
            compound=False, ladder_trigger_pct=0.5, ladder_step_pct=0.25, ladder_trail_pct=0.25,
            last_entry_time=time(12, 0), min_range_pct=0.002, require_vwap_side=True, confirm_bars=2,
            fib_target_ext=2.0)


def variants() -> dict[str, dict]:
    v = {"live rules": {}}
    for t in (time(11, 0), time(12, 0), time(12, 30), time(13, 0), time(14, 0)):
        v[f"time stop {t:%H:%M}"] = dict(time_stop=t)
    v["time stop 12:30, below +25%"] = dict(time_stop=time(12, 30), time_stop_min_gain_pct=0.25)
    for pct in (0.4, 0.5, 0.6, 0.7):
        v[f"premium stop -{pct:.0%}"] = dict(use_stop_loss=True, stop_loss_pct_of_premium=pct)
    v["-50% stop + time stop 13:00"] = dict(use_stop_loss=True, stop_loss_pct_of_premium=0.5, time_stop=time(13, 0))
    return v


def run(task):
    name, kw, stress = task
    spy = load_spy()
    vol = vix_vol_series(spy.index, load("data/vix_5min.csv")["close"])
    extra = {}
    if stress == "IV +25%":
        vol = vol * 1.25
    elif stress == "slip .05":
        extra = dict(slippage_per_share=0.05)
    r = run_options_backtest(bars=spy, strategy=OpeningRangeBreakoutStrategy(15, 5), vol_override=vol,
                             **{**LIVE, **kw, **extra})
    return name, stress, [(t.entry_time, t.pnl, t.exit_reason) for t in r.closed_trades]


def stats(tr: list) -> dict:
    pn = np.array([p for _, p, _ in tr])
    if not len(pn):
        return dict(trades=0, win=0.0, net=0, pf=0.0, worst=0, dd=0)
    w, gl = pn[pn > 0].sum(), -pn[pn <= 0].sum()
    eq = 100_000 + pn.cumsum()
    peak = np.maximum.accumulate(np.concatenate([[100_000], eq]))[1:]
    return dict(trades=len(pn), win=round(100 * (pn > 0).mean(), 1), net=round(pn.sum()),
                pf=round(w / gl, 2) if gl else np.inf, worst=round(pn.min()), dd=round(100 * (eq / peak - 1).min(), 1))


def main() -> None:
    V = variants()
    tasks = [(n, kw, "base") for n, kw in V.items()] + [(n, kw, s) for n, kw in V.items()
                                                         for s in ("IV +25%", "slip .05")]
    with ProcessPoolExecutor() as pool:
        out = {(n, s): tr for n, s, tr in pool.map(run, tasks)}
    days = sorted({t.date() for t, _, _ in out[("live rules", "base")]})
    split = days[int(len(days) * TRAIN_FRAC)]
    print(f"{len(days)} trade days {days[0]} .. {days[-1]}; train before {split}, test from {split}\n")

    rows = []
    for n in V:
        tr = out[(n, "base")]
        a, b = stats([x for x in tr if x[0].date() < split]), stats([x for x in tr if x[0].date() >= split])
        sa, sb = (stats([x for x in out[(n, s)] if x[0].date() >= split]) for s in ("IV +25%", "slip .05"))
        rows.append({"variant": n, "train n": a["trades"], "train PF": a["pf"], "train net": a["net"],
                     "train DD%": a["dd"], "test n": b["trades"], "test win%": b["win"], "test PF": b["pf"],
                     "test net": b["net"], "test DD%": b["dd"], "test worst": b["worst"],
                     "test PF IV+25%": sa["pf"], "test PF slip.05": sb["pf"]})
    g = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    print("Ranked on TRAIN profit factor (test columns are for reading, not for choosing):")
    print(g.sort_values("train PF", ascending=False).to_string(index=False))
    best = g[g["train n"] >= MIN_TRADES].sort_values("train PF", ascending=False).iloc[0]
    live = g[g.variant == "live rules"].iloc[0]
    print(f"\nChosen on train: {best.variant}")
    print(f"  TEST: PF {best['test PF']} vs live {live['test PF']}; net ${best['test net']:,} vs ${live['test net']:,}; "
          f"max DD {best['test DD%']}% vs {live['test DD%']}%; worst trade ${best['test worst']:,} vs ${live['test worst']:,}")
    reasons = pd.Series([r for _, _, r in out[("live rules", "base")]]).value_counts()
    print(f"\nLive rules, exits over all days: {reasons.to_dict()}")


if __name__ == "__main__":
    main()
