"""RSI(2): which loss cuts help? Walk-forward test on 15 years of S&P 500
daily bars (2026-11-03, after CBIO -$577 and the Oct 12-22 semiconductor
cluster).

The live rules (run_rsi2_bot.py), as a portfolio:
  - After the close: BUY when RSI(2) < 5 and the close is above its 200-day
    SMA, unless today's volume is >= 3x its 20-day average; the most
    oversold first; at most 5 positions of $2,000 (max_position_value);
    no new entries on Fridays except the fixed watchlist (the scans are
    skipped). Filled at the next open.
  - SELL at the next open after RSI(2) closes above 65.
  - 10% stop from the signal close, resting at the broker: filled at the
    stop, or at the open when the stock gaps through it.
Costs: $0.01/share slippage each side, IBKR fixed commission ($0.005/share,
$1 minimum) each side.

Variants change one thing: the stop (none / 5 / 7 / 15 / 20% / 3x ATR), a
time exit (5 or 10 trading days), the exit (close above the 5-day SMA, the
classic Connors exit), a sector cap (1 or 2 positions per sector), or
skipping a stock that fell more than 7% on the signal day (a news drop).

Walk-forward: chosen on the first 60% of the days (profit factor, at least
200 trades), judged only on the last 40%. Universe: today's S&P 500
(survivorship bias: stocks that left the index aren't in it, so absolute
returns are flattering; the comparison between variants is what counts).
Not modelled: the step-up profit stop (+11% -> +10%), the top-movers scan.

    python scripts/rsi2_loss_cut_test.py             # downloads once, caches data/sp500_daily.pkl
    python scripts/rsi2_loss_cut_test.py --refresh
    python scripts/rsi2_loss_cut_test.py --locks      # the step-up profit locks, with the 15% stop
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from tradingbot.config import settings
from tradingbot.indicators import rsi
from tradingbot.risk import ladder_stop, step_trailing_stop
from tradingbot.sp500_scanner import fetch_sp500_constituents

CACHE = Path("data/sp500_daily.pkl")
SLOTS, POSITION, SLIP, TRAIN_FRAC, MIN_TRADES = 5, 2_000.0, 0.01, 0.6, 200
WATCHLIST = {s.replace(".", "-") for s in settings.rsi2_symbols}


@dataclass(frozen=True)
class Rules:
    stop_pct: float | None = 0.10
    stop_atr: float | None = None
    max_days: int | None = None
    exit_sma5: bool = False
    sector_cap: int | None = None
    max_drop: float | None = None
    lock: str | None = None   # profit lock: "step10" (+11% -> +10%, +21% -> +20%, ...) or "ladder30"
                              # (+30% -> +15%, +45% -> +30%, ...); raised from each day's high, from the next day


def load(refresh: bool) -> dict[str, pd.DataFrame]:
    if CACHE.exists() and not refresh:
        return pd.read_pickle(CACHE)
    syms = fetch_sp500_constituents()["symbol"].tolist()
    raw = yf.download(syms, period="16y", interval="1d", group_by="ticker", auto_adjust=True,
                      threads=True, progress=False)
    out = {}
    for s in syms:
        if s in raw.columns.get_level_values(0):
            df = raw[s].rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
            if len(df) > 260:
                out[s] = df
    pd.to_pickle(out, CACHE)
    return out


def panels(data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Wide date x symbol frames of everything the rules need, known at each close."""
    f = {k: pd.DataFrame({s: d[k] for s, d in data.items()}) for k in ("open", "high", "low", "close", "volume")}
    c = f["close"]
    f["rsi"] = c.apply(lambda s: rsi(s.dropna(), 2)).reindex(c.index)
    f["sma200"], f["sma5"] = c.rolling(200).mean(), c.rolling(5).mean()
    f["vol_avg"] = f["volume"].shift(1).rolling(20).mean()
    tr = pd.concat([f["high"] - f["low"], (f["high"] - c.shift()).abs(), (f["low"] - c.shift()).abs()]).groupby(level=0).max()
    f["atr"] = tr.rolling(14).mean()
    f["ret"] = c.pct_change()
    return f


def commission(q: int) -> float:
    return max(1.0, 0.005 * q)


def simulate(f: dict[str, pd.DataFrame], sectors: dict[str, str], r: Rules) -> pd.DataFrame:
    days, syms = f["close"].index, f["close"].columns
    O, H, L, C = (f[k].to_numpy() for k in ("open", "high", "low", "close"))
    RSI, S200, S5, V, VA, ATR, RET = (f[k].to_numpy() for k in ("rsi", "sma200", "sma5", "volume", "vol_avg", "atr", "ret"))
    buy = (RSI < 5) & (C > S200) & ~(V >= 3 * VA)
    if r.max_drop is not None:
        buy &= ~(RET <= -r.max_drop)
    wl = np.array([s in WATCHLIST for s in syms])
    sec = np.array([sectors.get(s, "?") for s in syms])
    held: dict[int, dict] = {}
    pend_buy: list[int] = []
    pend_sell: set[int] = set()
    trades = []

    def close(j, i, px, why):
        p = held.pop(j)
        q = p["qty"]
        pnl = (px - SLIP - p["entry"]) * q - commission(q) - p["fee"]
        trades.append(dict(symbol=syms[j], entry_date=days[p["i"]], exit_date=days[i], days=i - p["i"],
                           pnl=pnl, ret=pnl / (p["entry"] * q), reason=why))

    for i in range(201, len(days)):
        for j in list(pend_sell):                      # exits signalled at yesterday's close
            if j in held and not np.isnan(O[i, j]):
                close(j, i, O[i, j], "exit")
        pend_sell.clear()
        for j in pend_buy:                             # entries signalled at yesterday's close
            if len(held) >= SLOTS or j in held or np.isnan(O[i, j]):
                continue
            sig = C[i - 1, j]
            q = int(POSITION // sig)
            if q < 1:
                continue
            stop = None
            if r.stop_pct is not None:
                stop = sig * (1 - r.stop_pct)
            elif r.stop_atr is not None:
                stop = sig - r.stop_atr * ATR[i - 1, j]
            held[j] = dict(i=i, qty=q, entry=O[i, j] + SLIP, fee=commission(q), stop=stop)
        pend_buy = []
        for j in list(held):                           # resting stop during the day
            p = held[j]
            if p["stop"] is not None and L[i, j] <= p["stop"]:
                close(j, i, min(p["stop"], O[i, j]), "stop")
        if r.lock:                                     # profit lock from today's high, working tomorrow
            for j, p in held.items():
                if np.isnan(H[i, j]):
                    continue
                cost = p["entry"]
                lvl = (step_trailing_stop(cost, H[i, j], 0.10, 0.01) if r.lock == "step10"
                       else ladder_stop(cost, H[i, j], 0.30, 0.15, 0.15))
                if lvl is not None and (p["stop"] is None or lvl > p["stop"]):
                    p["stop"] = lvl
        for j, p in held.items():                      # exit signals at the close
            if np.isnan(C[i, j]):
                continue
            if (C[i, j] > S5[i, j]) if r.exit_sma5 else (RSI[i, j] > 65):
                pend_sell.add(j)
            elif r.max_days is not None and i - p["i"] + 1 >= r.max_days:
                pend_sell.add(j)
        free = SLOTS - (len(held) - len(pend_sell))
        if free <= 0:
            continue
        cand = np.flatnonzero(buy[i])
        if days[i].weekday() == 4:
            cand = cand[wl[cand]]
        cand = [j for j in cand[np.argsort(RSI[i, cand])] if j not in held]
        counts = pd.Series([sec[j] for j in held if j not in pend_sell]).value_counts().to_dict()
        for j in cand:
            if len(pend_buy) >= free:
                break
            if r.sector_cap is not None and counts.get(sec[j], 0) >= r.sector_cap:
                continue
            pend_buy.append(j)
            counts[sec[j]] = counts.get(sec[j], 0) + 1
    return pd.DataFrame(trades)


def stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return dict(trades=0, win=0.0, avg=0.0, pf=0.0, net=0, worst=0, dd=0)
    t = t.sort_values("exit_date")
    w, gl = t.pnl[t.pnl > 0].sum(), -t.pnl[t.pnl <= 0].sum()
    eq = t.pnl.cumsum()
    return dict(trades=len(t), win=round(100 * (t.pnl > 0).mean(), 1), avg=round(100 * t.ret.mean(), 2),
                pf=round(w / gl, 2) if gl else np.inf, net=round(t.pnl.sum()), worst=round(t.pnl.min()),
                dd=round((eq - eq.cummax()).min()))


LOCKS = {
    "15% stop, no profit lock": Rules(stop_pct=0.15),
    "15% stop + current lock (+11% -> +10%, 10% steps)": Rules(stop_pct=0.15, lock="step10"),
    "15% stop + new lock (+30% -> +15%, 15% steps)": Rules(stop_pct=0.15, lock="ladder30"),
}

VARIANTS = {
    "live rules (10% stop, RSI > 65 exit)": Rules(),
    "no stop": Rules(stop_pct=None),
    "5% stop": Rules(stop_pct=0.05),
    "7% stop": Rules(stop_pct=0.07),
    "15% stop": Rules(stop_pct=0.15),
    "20% stop": Rules(stop_pct=0.20),
    "3x ATR stop": Rules(stop_pct=None, stop_atr=3.0),
    "time exit 5 days": Rules(max_days=5),
    "time exit 10 days": Rules(max_days=10),
    "exit close > 5-day SMA": Rules(exit_sma5=True),
    "max 1 per sector": Rules(sector_cap=1),
    "max 2 per sector": Rules(sector_cap=2),
    "skip a > 7% one-day drop": Rules(max_drop=0.07),
}


def main() -> None:
    data = load("--refresh" in sys.argv)
    sectors = dict(zip(*fetch_sp500_constituents()[["symbol", "sector"]].T.values))
    f = panels(data)
    days = f["close"].index
    split = days[201 + int((len(days) - 201) * TRAIN_FRAC)]
    print(f"{len(data)} stocks, {days[201].date()} .. {days[-1].date()}; train before {split.date()}, test from it\n")
    rows, live_trades = [], None
    for name, r in (LOCKS if "--locks" in sys.argv else VARIANTS).items():
        t = simulate(f, sectors, r)
        if live_trades is None:
            live_trades = t
        a, b = stats(t[t.entry_date < split]), stats(t[t.entry_date >= split])
        rows.append({"variant": name, "train n": a["trades"], "train PF": a["pf"], "train avg%": a["avg"],
                     "train DD$": a["dd"], "test n": b["trades"], "test win%": b["win"], "test avg%": b["avg"],
                     "test PF": b["pf"], "test net$": b["net"], "test worst$": b["worst"], "test DD$": b["dd"]})
        print(f"  {name}: done", flush=True)
    g = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    print("\nRanked on TRAIN profit factor (test columns are for reading, not for choosing):")
    print(g.sort_values("train PF", ascending=False).to_string(index=False))
    best = g[g["train n"] >= MIN_TRADES].sort_values("train PF", ascending=False).iloc[0]
    live = g.iloc[0]
    print(f"\nChosen on train: {best.variant}")
    print(f"  TEST: PF {best['test PF']} vs live {live['test PF']}; avg {best['test avg%']}% vs {live['test avg%']}% "
          f"per trade; net ${best['test net$']:,} vs ${live['test net$']:,}; max DD ${best['test DD$']:,} vs "
          f"${live['test DD$']:,}")
    print(f"\nLive rules, exits over all days: {live_trades.reason.value_counts().to_dict()}; "
          f"stops: {stats(live_trades[live_trades.reason == 'stop'])}")


if __name__ == "__main__":
    main()
