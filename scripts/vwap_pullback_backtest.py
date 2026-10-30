"""Backtest a VWAP + EMA pullback scalp on the S&P 500 (1/2/3/5-min bars).

The rules most often given for candle scalping (e.g. algotest.in's
"scalping strategy" guide), written out so they can be tested:

  long : close > session VWAP and EMA9 > EMA21 (trend up); the bar's low
         touches the EMA9 but it closes back above it, as a green candle
         (close > open)
  short: the mirror image (close < VWAP, EMA9 < EMA21, high touches EMA9,
         closes back below it, red candle)
  entry: next bar's open; signal bars 9:40-15:00
  stop : 1 cent beyond the signal bar's low (long) / high (short)
  exit : target at 1R / 1.5R / 2R, else flat at the 15:55 bar's open
         (a bar touching both stop and target counts as the stop)
  one trade per symbol per side per day; risk at least 0.05 % of price

Sizing and costs are the Stock ORB backtest's: $50 risk per trade, $4,000
max position; $0.01/share slippage on market fills (entry, stop, flatten),
IBKR fixed commission ($0.005/share, $1 minimum) each side. Two views:
every signal (the edge), and a realistic day (max 3 trades, $150 daily
loss budget, signals taken in time order, most dollars traded first).
"before costs" adds the commissions and slippage back.

The first two thirds of the sessions pick the best variant; the last third
is kept out as the check.

Data: with no --minutes, the 60 days of 5-min bars in data/stock_orb_5m.pkl
(scripts/stock_orb_backtest.py downloads it). With --minutes, Yahoo's 1-min
bars (it keeps only the last ~30 days; cached in data/scalp_1m.pkl), and
2/3/5-min candles built from them, so every timeframe covers the same days.

    python scripts/vwap_pullback_backtest.py                 # 5-min, 60 days
    python scripts/vwap_pullback_backtest.py --minutes 1,2,3,5
    python scripts/vwap_pullback_backtest.py --minutes 1 --refresh
"""
from __future__ import annotations

import argparse
from datetime import date, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from tradingbot.sp500_scanner import fetch_sp500_constituents
from tradingbot.strategy.stock_orb import regular_session, share_quantity

CACHE_5M = Path("data/stock_orb_5m.pkl")
CACHE_1M = Path("data/scalp_1m.pkl")
MAX_LOSS, MAX_ORDER, DAILY_LOSS, MAX_TRADES = 50.0, 4_000.0, 150.0, 3
SLIP, MIN_RISK_PCT = 0.01, 0.05
FIRST, LAST, FLAT = time(9, 40), time(15, 0), time(15, 55)
TARGETS = (1.0, 1.5, 2.0)


def commission(shares: int) -> float:
    return max(1.0, 0.005 * shares)


def load_1m(refresh: bool) -> dict[str, pd.DataFrame]:
    """Yahoo's 1-min bars for the S&P 500, last ~29 days, fetched 7 days at
    a time (Yahoo's limit per 1-min request)."""
    if CACHE_1M.exists() and not refresh:
        return pd.read_pickle(CACHE_1M)
    symbols = fetch_sp500_constituents()["symbol"].tolist()
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=29)
    parts: dict[str, list[pd.DataFrame]] = {s: [] for s in symbols}
    while start < end:
        stop = min(end, start + timedelta(days=7))
        raw = yf.download(symbols, start=start.isoformat(), end=stop.isoformat(), interval="1m",
                          group_by="ticker", auto_adjust=False, prepost=False, threads=True, progress=False)
        for s in symbols:
            if raw is None or raw.empty or s not in raw.columns.get_level_values(0):
                continue
            df = raw[s].rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
            if not df.empty:
                df.index = df.index.tz_convert("America/New_York")
                parts[s].append(regular_session(df))
        start = stop
    out = {}
    for s, p in parts.items():
        if p:
            df = pd.concat(p).sort_index()
            out[s] = df[~df.index.duplicated()]
    CACHE_1M.parent.mkdir(exist_ok=True)
    pd.to_pickle(out, CACHE_1M)
    return out


def resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if minutes == 1:
        return df
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return df.resample(f"{minutes}min", label="left", closed="left").agg(agg).dropna()


def signals(sym: str, df: pd.DataFrame) -> list[dict]:
    """Every signal for one symbol: the first per side per day."""
    df = df.copy()
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    day = df.index.date
    tp = (df["high"] + df["low"] + df["close"]) / 3
    df["vwap"] = (tp * df["volume"]).groupby(day).cumsum() / df["volume"].groupby(day).cumsum()
    df["dollars"] = (df["close"] * df["volume"]).groupby(day).cumsum()
    t = df.index.time
    window = (t >= FIRST) & (t <= LAST)
    long_sig = (window & (df["close"] > df["vwap"]) & (df["ema9"] > df["ema21"])
                & (df["low"] <= df["ema9"]) & (df["close"] > df["ema9"]) & (df["close"] > df["open"]))
    short_sig = (window & (df["close"] < df["vwap"]) & (df["ema9"] < df["ema21"])
                 & (df["high"] >= df["ema9"]) & (df["close"] < df["ema9"]) & (df["close"] < df["open"]))
    out = []
    for side, mask in (("long", long_sig), ("short", short_sig)):
        idx = np.flatnonzero(mask.values)
        for d, i in pd.Series(idx, dtype=int).groupby(day[idx]).first().items():
            if i + 1 >= len(df) or df.index[i + 1].date() != d:
                continue
            out.append({"sym": sym, "date": d, "side": side, "ts": df.index[i + 1], "i": i,
                        "dollars": df["dollars"].iloc[i]})
    return out


def arrays(df: pd.DataFrame) -> dict:
    return {"o": df["open"].to_numpy(), "h": df["high"].to_numpy(), "l": df["low"].to_numpy(),
            "c": df["close"].to_numpy(), "d": np.array(df.index.date), "t": np.array(df.index.time)}


def simulate(a: dict, sig: dict, target_r: float) -> dict | None:
    i, long_ = sig["i"], sig["side"] == "long"
    o, h, l, c, d, t = a["o"], a["h"], a["l"], a["c"], a["d"], a["t"]
    entry = o[i + 1] + (SLIP if long_ else -SLIP)
    stop = round(l[i] - 0.01, 2) if long_ else round(h[i] + 0.01, 2)
    risk = (entry - stop) if long_ else (stop - entry)
    if risk <= 0 or risk / entry * 100 < MIN_RISK_PCT:
        return None
    qty = share_quantity(entry, entry - risk, MAX_LOSS, MAX_ORDER)
    if qty < 1:
        return None
    target = entry + target_r * risk if long_ else entry - target_r * risk
    exit_px, outcome, j = None, "flat", i + 1
    while j < len(o) and d[j] == sig["date"]:
        if t[j] >= FLAT:
            exit_px = o[j] - SLIP if long_ else o[j] + SLIP
            break
        if (l[j] <= stop) if long_ else (h[j] >= stop):
            exit_px = (min(o[j], stop) - SLIP) if long_ else (max(o[j], stop) + SLIP)
            outcome = "stop"
            break
        if (h[j] >= target) if long_ else (l[j] <= target):
            exit_px, outcome = target, "target"
            break
        j += 1
    if exit_px is None:  # day ended before 15:55 (half day)
        exit_px = c[j - 1] - SLIP if long_ else c[j - 1] + SLIP
    gross = (exit_px - entry) * qty if long_ else (entry - exit_px) * qty
    fees = 2 * commission(qty)
    slip = SLIP * qty * (1 if outcome == "target" else 2)  # entry, plus the exit unless it's the limit
    net = gross - fees
    return {**sig, "target_r": target_r, "qty": qty, "entry": entry, "stop": stop, "outcome": outcome,
            "net": net, "r": net / (risk * qty), "r_gross": (net + fees + slip) / (risk * qty)}


def capped_day(trades: pd.DataFrame) -> pd.DataFrame:
    """Max 3 trades a day and the $150 loss budget, in time order."""
    kept = []
    for _, day in trades.sort_values(["ts", "dollars"], ascending=[True, False]).groupby("date"):
        n, loss = 0, 0.0
        for _, t in day.iterrows():
            if n >= MAX_TRADES or loss + MAX_LOSS > DAILY_LOSS:
                break
            kept.append(t)
            n += 1
            loss += max(0.0, -t["net"])
    return pd.DataFrame(kept)


def summary(t: pd.DataFrame) -> str:
    if t.empty:
        return "no trades"
    wins = (t["net"] > 0).mean() * 100
    pf = t.loc[t["net"] > 0, "net"].sum() / max(1e-9, -t.loc[t["net"] < 0, "net"].sum())
    return (f"{len(t):5d} trades  win {wins:4.1f}%  avg {t['r'].mean():+.3f}R  PF {pf:4.2f}  "
            f"net ${t['net'].sum():+10,.2f}  (before costs {t['r_gross'].mean():+.3f}R)")


def run(data: dict[str, pd.DataFrame], label: str) -> None:
    days = sorted({d for df in data.values() for d in set(df.index.date)})
    split = days[len(days) * 2 // 3]
    print(f"=== {label}: {len(data)} symbols, {len(days)} sessions {days[0]} .. {days[-1]}; "
          f"in-sample before {split}, out-of-sample from {split}\n")
    rows = []
    for sym, df in data.items():
        a = arrays(df)
        for s in signals(sym, df):
            for target_r in TARGETS:
                r = simulate(a, s, target_r)
                if r is not None:
                    rows.append(r)
    all_t = pd.DataFrame(rows)
    all_t["oos"] = all_t["date"] >= split

    best = None
    for side in ("long", "short", "both"):
        for target_r in TARGETS:
            t = all_t[all_t["target_r"] == target_r]
            if side != "both":
                t = t[t["side"] == side]
            ins, oos = t[~t["oos"]], t[t["oos"]]
            print(f"{side:5s} {target_r:.1f}R  every signal  IN  {summary(ins)}")
            print(f"{'':10s} every signal  OUT {summary(oos)}")
            print(f"{'':10s} capped day    IN  {summary(capped_day(ins))}")
            print(f"{'':10s} capped day    OUT {summary(capped_day(oos))}\n")
            if best is None or ins["r"].mean() > best[0]:
                best = (ins["r"].mean(), side, target_r)
    _, side, target_r = best
    print(f"Best in-sample (every signal, avg R): {side} {target_r:.1f}R")
    t = all_t[all_t["target_r"] == target_r]
    if side != "both":
        t = t[t["side"] == side]
    print(f"  out-of-sample every signal: {summary(t[t['oos']])}")
    print(f"  out-of-sample capped day:   {summary(capped_day(t[t['oos']]))}\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", help="comma-separated bar sizes built from 1-min bars, e.g. 1,2,3,5")
    ap.add_argument("--refresh", action="store_true", help="re-download the 1-min bars")
    args = ap.parse_args()
    if not args.minutes:
        run(pd.read_pickle(CACHE_5M), "5-min bars (60 days)")
        return
    one = load_1m(args.refresh)
    for m in (int(x) for x in args.minutes.split(",")):
        run({s: resample(df, m) for s, df in one.items()}, f"{m}-min bars")


if __name__ == "__main__":
    main()
