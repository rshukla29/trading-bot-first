"""ORB (SPY): would a debit spread or a 1-2 day option stop a failed
breakout from losing the whole premium? (2026-11-09, after 2026-11-06's
741C went from $3.02 to $0: one full loss wipes out about four winners.)

Every variant is the live rule set (15-min range, noon cutoff, the three
entry filters, step-up stop, 200% Fibonacci target, 3:55 flatten — always
flat at the close, whatever the expiry) with one change to what is bought:
  - debit spread $W: buy the at-the-money strike, sell the one $W further
    out (both legs priced, slippage and commission on both);
  - 1DTE / 2DTE: the option expiring 1 / 2 trading days out, sold at 3:55
    with its time value left.
Same engine, VIX-calibrated pricing and costs as scripts/orb_loss_cut_test.py
(the VIX model is calibrated to 0DTE fills; for 1-2DTE its last-hour
ramp is left out, and a second run prices them at plain VIX).
Sizing: 1% risk, 10 contracts max, no compounding — so cheaper structures
are compared per contract, not boosted by buying more of them.
Walk-forward: chosen on the first 60% of sessions (profit factor, at least
100 trades), judged on the last 40%; stress: options 25% pricier, $0.05
slippage per leg.

    python scripts/orb_structure_test.py
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from scripts.credit_orb_backtest import load, load_spy
from scripts.orb_loss_cut_test import LIVE, MIN_TRADES, TRAIN_FRAC
from tradingbot.options.backtest import run_options_backtest
from tradingbot.options.orb_strategy import OpeningRangeBreakoutStrategy
from tradingbot.options.pricing import VIX_IV_MULTIPLIER, vix_vol_series

VARIANTS = {
    "live rules (0DTE single option)": {},
    "debit spread $2": dict(spread_width=2.0),
    "debit spread $3": dict(spread_width=3.0),
    "debit spread $5": dict(spread_width=5.0),
    "1DTE single option": dict(dte_days=1),
    "2DTE single option": dict(dte_days=2),
    "1DTE debit spread $3": dict(dte_days=1, spread_width=3.0),
    "1DTE single option, priced at plain VIX": dict(dte_days=1, vol="vix"),
    "2DTE single option, priced at plain VIX": dict(dte_days=2, vol="vix"),
}


def run(task):
    name, stress = task
    spy = load_spy()
    vix = load("data/vix_5min.csv")["close"]
    kw = dict(VARIANTS[name])
    mode = kw.pop("vol", "model")
    if kw.get("dte_days"):
        # The 0DTE model's last-hour ramp (pin risk near expiry) doesn't
        # apply to an option with days left: without it, a 1DTE sold at 3:55
        # was priced at an inflated vol and showed fake gains.
        vol = vix.reindex(spy.index).ffill() / 100 * (1.0 if mode == "vix" else VIX_IV_MULTIPLIER)
    else:
        vol = vix_vol_series(spy.index, vix)
    extra = {}
    if stress == "IV +25%":
        vol = vol * 1.25
    elif stress == "slip .05":
        extra = dict(slippage_per_share=0.05)
    r = run_options_backtest(bars=spy, strategy=OpeningRangeBreakoutStrategy(15, 5), vol_override=vol,
                             **{**LIVE, **kw, **extra})
    return name, stress, [(t.entry_time, t.pnl, t.entry_premium * t.contracts * 100) for t in r.closed_trades]


def stats(tr: list) -> dict:
    if not tr:
        return dict(trades=0, win=0.0, net=0, pf=0.0, avg_win=0, avg_loss=0, full_loss=0.0, worst_day=0, dd=0.0)
    pn = np.array([p for _, p, _ in tr])
    cost = np.array([c for _, _, c in tr])
    w, gl = pn[pn > 0].sum(), -pn[pn <= 0].sum()
    eq = 100_000 + pn.cumsum()
    peak = np.maximum.accumulate(np.concatenate([[100_000], eq]))[1:]
    by_day = pd.Series(pn, index=[t.date() for t, _, _ in tr]).groupby(level=0).sum()
    return dict(trades=len(pn), win=round(100 * (pn > 0).mean(), 1), net=round(pn.sum()),
                pf=round(w / gl, 2) if gl else np.inf,
                avg_win=round(pn[pn > 0].mean()) if (pn > 0).any() else 0,
                avg_loss=round(pn[pn <= 0].mean()) if (pn <= 0).any() else 0,
                full_loss=round(100 * (pn <= -0.8 * cost).mean(), 1),
                worst_day=round(by_day.min()), dd=round(100 * (eq / peak - 1).min(), 1))


def main() -> None:
    tasks = [(n, s) for n in VARIANTS for s in ("base", "IV +25%", "slip .05")]
    with ProcessPoolExecutor() as pool:
        out = {(n, s): tr for n, s, tr in pool.map(run, tasks)}
    days = sorted({t.date() for t, *_ in out[("live rules (0DTE single option)", "base")]})
    split = days[int(len(days) * TRAIN_FRAC)]
    print(f"{len(days)} trade days {days[0]} .. {days[-1]}; train before {split}, test from {split}\n")

    rows = []
    for n in VARIANTS:
        tr = out[(n, "base")]
        a, b = stats([x for x in tr if x[0].date() < split]), stats([x for x in tr if x[0].date() >= split])
        sa, sb = (stats([x for x in out[(n, s)] if x[0].date() >= split]) for s in ("IV +25%", "slip .05"))
        rows.append({"variant": n, "train n": a["trades"], "train PF": a["pf"], "train net": a["net"],
                     "test n": b["trades"], "test win%": b["win"], "test PF": b["pf"], "test net": b["net"],
                     "avg win": b["avg_win"], "avg loss": b["avg_loss"], "lost >=80%": b["full_loss"],
                     "worst day": b["worst_day"], "test DD%": b["dd"],
                     "PF IV+25%": sa["pf"], "PF slip.05": sb["pf"]})
    g = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    print("Ranked on TRAIN profit factor (test columns are for reading, not for choosing):")
    print(g.sort_values("train PF", ascending=False).to_string(index=False))
    best = g[g["train n"] >= MIN_TRADES].sort_values("train PF", ascending=False).iloc[0]
    live = g.iloc[0]
    print(f"\nChosen on train: {best.variant}")
    print(f"  TEST: PF {best['test PF']} vs live {live['test PF']}; net ${best['test net']:,} vs ${live['test net']:,}; "
          f"avg loss ${best['avg loss']:,} vs ${live['avg loss']:,}; worst day ${best['worst day']:,} vs "
          f"${live['worst day']:,}")


if __name__ == "__main__":
    main()
