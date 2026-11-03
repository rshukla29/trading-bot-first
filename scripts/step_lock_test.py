"""MA-crossover: which step-up profit lock? (2026-11-03, owner's proposal:
nothing until +30%, then the stop trails 15 points behind each 15% step.)

MA-crossover's live rules (strategy/factory.py build_strategy, the .env
settings) on its own WATCHLIST, 15 years of daily bars, one symbol at a
time: signal at the close, filled at the next open; $2,000 positions; 10%
stop from the entry, resting at the broker (filled at the stop, or at the
open on a gap through it). Profit locks, raised from each day's high and
working from the next day (daily bars can't show what came first within a
day; the same for every lock):
  none     the 10% stop only
  current  +11% -> +10%, +21% -> +20%, ... (risk.step_trailing_stop, live)
  new      +30% -> +15%, +45% -> +30%, ... (risk.ladder_stop 30/15/15)
Costs: $0.01/share slippage each side, IBKR fixed commission each side.
Split by entry date: first 60% of the days / last 40%. Nothing is tuned.

The watchlist is today's (NVDA, PLTR, ... were picked knowing how they
did), so absolute returns are flattering; compare the locks with each other.

    python scripts/step_lock_test.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from tradingbot.config import settings
from tradingbot.risk import ladder_stop, step_trailing_stop
from tradingbot.strategy.factory import build_strategy

CACHE = Path("data/ma_watchlist_daily.pkl")
POSITION, SLIP, STOP, TRAIN_FRAC = 2_000.0, 0.01, 0.10, 0.6
LOCKS = {"none": None, "current (+11% -> +10%, 10% steps)": "step10", "new (+30% -> +15%, 15% steps)": "ladder30"}


def load() -> dict[str, pd.DataFrame]:
    if CACHE.exists():
        return pd.read_pickle(CACHE)
    syms = [s.replace(" ", "-") for s in settings.symbols]
    raw = yf.download(syms, period="16y", interval="1d", group_by="ticker", auto_adjust=True, progress=False)
    out = {s: raw[s].rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna() for s in syms
           if s in raw.columns.get_level_values(0)}
    out = {s: d for s, d in out.items() if len(d) > 300}
    pd.to_pickle(out, CACHE)
    return out


def signals(bars: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """BUY / EXIT at each close, from the bars up to that close only."""
    strat = build_strategy(settings)
    n = len(bars)
    buy, exit_ = np.zeros(n, bool), np.zeros(n, bool)
    for i in range(strat.min_history, n):
        a = strat.generate_signal("X", bars.iloc[max(0, i - 400): i + 1]).action
        buy[i], exit_[i] = a == "BUY", a == "EXIT"
    return buy, exit_


def commission(q: int) -> float:
    return max(1.0, 0.005 * q)


def trades_for(sym: str, bars: pd.DataFrame, buy: np.ndarray, ex: np.ndarray, lock: str | None) -> list[dict]:
    O, H, L = (bars[k].to_numpy() for k in ("open", "high", "low"))
    out, pos, pend_buy, pend_sell = [], None, False, False

    def close(i, px, why):
        q, e = pos["qty"], pos["entry"]
        pnl = (px - SLIP - e) * q - commission(q) * 2
        out.append(dict(symbol=sym, entry_date=bars.index[pos["i"]], exit_date=bars.index[i], days=i - pos["i"],
                        pnl=pnl, ret=pnl / (e * q), peak=pos["peak"] / e - 1, reason=why))

    for i in range(len(bars)):
        if pend_sell and pos:
            close(i, O[i], "exit")
            pos = None
        pend_sell = False
        if pend_buy and pos is None:
            e = O[i] + SLIP
            q = int(POSITION // e)
            if q:
                pos = dict(i=i, qty=q, entry=e, stop=e * (1 - STOP), peak=e)
        pend_buy = False
        if pos:
            if L[i] <= pos["stop"]:
                close(i, min(pos["stop"], O[i]), "stop" if pos["stop"] < pos["entry"] else "lock")
                pos = None
            else:
                pos["peak"] = max(pos["peak"], H[i])
                if lock:
                    lvl = (step_trailing_stop(pos["entry"], H[i], 0.10, 0.01) if lock == "step10"
                           else ladder_stop(pos["entry"], H[i], 0.30, 0.15, 0.15))
                    if lvl is not None and lvl > pos["stop"]:
                        pos["stop"] = lvl
                pend_sell = bool(ex[i])
        if pos is None and buy[i]:
            pend_buy = True
    return out


def stats(t: pd.DataFrame) -> str:
    if t.empty:
        return "no trades"
    w, gl = t.pnl[t.pnl > 0].sum(), -t.pnl[t.pnl <= 0].sum()
    eq = t.sort_values("exit_date").pnl.cumsum()
    return (f"{len(t):4d} trades  win {100 * (t.pnl > 0).mean():4.1f}%  avg {100 * t.ret.mean():+5.2f}%  "
            f"PF {w / gl if gl else float('inf'):4.2f}  net ${t.pnl.sum():+8,.0f}  max DD ${(eq - eq.cummax()).min():,.0f}  "
            f"best ${t.pnl.max():,.0f}")


def main() -> None:
    data = load()
    print(f"{len(data)} watchlist stocks: {sorted(data)}; computing MA-crossover signals...", flush=True)
    sig = {s: signals(d) for s, d in data.items()}
    days = sorted({d for b in data.values() for d in b.index})
    split = days[int(len(days) * TRAIN_FRAC)]
    print(f"{days[0].date()} .. {days[-1].date()}; first 60% before {split.date()}\n")
    for name, lock in LOCKS.items():
        t = pd.DataFrame([r for s, d in data.items() for r in trades_for(s, d, *sig[s], lock)])
        print(f"--- lock: {name}   exits {t.reason.value_counts().to_dict()}")
        print(f"  first 60%  {stats(t[t.entry_date < split])}")
        print(f"  last 40%   {stats(t[t.entry_date >= split])}")
        big = t[t.peak >= 0.30]
        print(f"  trades that were up 30%+ at some point: {len(big)}, they made ${big.pnl.sum():,.0f} "
              f"(avg kept {100 * big.ret.mean():.0f}% of a {100 * big.peak.mean():.0f}% avg peak)\n")


if __name__ == "__main__":
    main()
