"""ORB-0DTE-HP backtest: the rules in options/orb_hp_strategy.py on 5-minute
SPY candles, trading SPY 0DTE options like the live bot.

    python scripts/orb_hp_backtest.py              # the cached 5-min bars (data/spy_5min.csv, vix_5min.csv)
    python scripts/orb_hp_backtest.py --update     # first append the bars since the cache ends (TWS, read-only)
    python scripts/orb_hp_backtest.py --slippage 0.03

Simulation:
  - a signal on a completed candle enters at the next candle's open (the bot
    buys a few seconds after the close); entries 9:35-15:00, one position at
    a time, at most 6 a day (also shown without the cap);
  - the option: 0DTE, strike nearest SPY, priced with Black-Scholes at the
    VIX-calibrated volatility the ORB backtest uses (no historical option
    quotes on this account: real fills can differ);
  - exit at the next open after the first opposite-colour candle (red for a
    call, green for a put), or at 15:55.
Costs: slippage per share per side and $0.65 per contract per side. Per contract.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tradingbot.config import settings  # noqa: E402
from tradingbot.options.orb_hp_strategy import CALL, PUT, HpParams, candle_exit, indicators, signal_at  # noqa: E402
from tradingbot.options.pricing import bs_price, vix_calibrated_vol  # noqa: E402

RESEARCH_CLIENT_ID = 94
FIRST, LAST, EOD = time(9, 35), time(15, 0), time(15, 55)
RATE = 0.04


def load(name: str) -> pd.DataFrame:
    df = pd.read_csv(f"data/{name}_5min.csv", index_col=0)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert("America/New_York")
    return df


def update(name: str, kind: str) -> None:
    """Append the 5-min bars from the cache's end to now (1-month requests)."""
    from ib_async import IB, Index, Stock, util
    df = load(name)
    ib = IB()
    ib.connect(settings.ibkr_host, settings.ibkr_port, clientId=RESEARCH_CLIENT_ID, readonly=True, timeout=15)
    try:
        c = Stock(name.upper(), "SMART", "USD") if kind == "stock" else Index(name.upper(), "CBOE")
        ib.qualifyContracts(c)
        chunks, end = [], ""
        while True:
            bars = util.df(ib.reqHistoricalData(c, endDateTime=end, durationStr="1 M", barSizeSetting="5 mins",
                                                whatToShow="TRADES", useRTH=kind == "stock", formatDate=2, timeout=300))
            if bars is None or bars.empty:
                break
            bars = bars.set_index("date")
            bars.index = pd.to_datetime(bars.index, utc=True).tz_convert("America/New_York")
            chunks.append(bars[[k for k in df.columns if k in bars.columns]])
            print(f"  {name}: {len(bars)} bars {bars.index[0].date()} .. {bars.index[-1].date()}")
            if bars.index[0] <= df.index[-1]:
                break
            end = bars.index[0].tz_convert("UTC").strftime("%Y%m%d-%H:%M:%S")
    finally:
        ib.disconnect()
    out = pd.concat([df, *chunks]).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    out.to_csv(f"data/{name}_5min.csv")


@dataclass(frozen=True)
class Variant:
    name: str
    sides: str = "CP"
    stop: float | None = None
    max_trades: int | None = 6


VARIANTS = [
    Variant("As specified: calls and puts, max 6/day"),
    Variant("Calls only", sides="C"),
    Variant("Puts only", sides="P"),
    Variant("No daily cap", max_trades=None),
    Variant("+ 30% stop loss", stop=0.30),
]


def premium(spot, strike, ts, vix, right):
    mins = max(1.0, ((ts.normalize() + pd.Timedelta(hours=16)) - ts).total_seconds() / 60)
    return bs_price(spot, strike, mins / (365.25 * 24 * 60), RATE, vix_calibrated_vol(vix, mins), right)


def simulate(spy: pd.DataFrame, vix: pd.Series, v: Variant, slip: float, comm: float) -> pd.DataFrame:
    p = HpParams()
    ind = indicators(spy, p)
    sig = [signal_at(ind, i, p) for i in range(len(ind))]
    o, h, l, c = (spy[k].to_numpy() for k in ("open", "high", "low", "close"))
    idx = spy.index
    vx = vix.reindex(idx).ffill().bfill().to_numpy()
    trades, pos, per_day = [], None, {}

    def close_pos(spot, ts, v_, reason, prem=None):
        nonlocal pos
        px = (prem if prem is not None else premium(spot, pos["k"], ts, v_, pos["r"])) - slip
        trades.append(dict(entry=pos["ts"], exit=ts, right=pos["r"], reason=reason,
                           pnl=(max(px, 0.0) - pos["px"]) * 100 - 2 * comm))
        pos = None

    for i in range(1, len(idx)):
        ts, t, d = idx[i], idx[i].time(), idx[i].date()
        vix_now = vx[i - 1]
        if pos is not None:
            if pos["day"] != d:
                close_pos(o[i], ts, vix_now, "eod")
            elif pos.get("exit") or t >= EOD:
                close_pos(o[i], ts, vix_now, "candle" if pos.get("exit") else "3:55pm")
        if pos is not None and v.stop:
            bad = l[i] if pos["r"] == CALL else h[i]
            lvl = pos["px0"] * (1 - v.stop)
            if premium(bad, pos["k"], ts, vix_now, pos["r"]) <= lvl:
                close_pos(bad, ts, vix_now, "stop", prem=lvl)
        if pos is not None and candle_exit(pos["r"], o[i], c[i]):
            pos["exit"] = True
        want = sig[i - 1]
        if pos is None and want and want in v.sides and idx[i - 1].date() == d and FIRST <= t < LAST \
                and (v.max_trades is None or per_day.get(d, 0) < v.max_trades):
            k = round(o[i])
            px0 = premium(o[i], k, ts, vix_now, want)
            pos = dict(k=k, r=want, px0=px0, px=px0 + slip, ts=ts, day=d)
            per_day[d] = per_day.get(d, 0) + 1
            if candle_exit(want, o[i], c[i]):      # the entry candle itself closes the wrong colour
                pos["exit"] = True
    return pd.DataFrame(trades)


def stats(t: pd.DataFrame) -> dict:
    pnl = t["pnl"]
    eq = pnl.cumsum()
    win, loss = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    return dict(n=len(t), win=100 * (pnl > 0).mean(), avg=pnl.mean(), pf=win / loss if loss else np.inf,
                total=pnl.sum(), dd=(eq - eq.cummax()).min())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--slippage", type=float, default=0.02)
    ap.add_argument("--commission", type=float, default=0.65)
    a = ap.parse_args()
    if a.update:
        update("spy", "stock")
        update("vix", "index")
    spy = load("spy").between_time("09:30", "15:59")
    vix = load("vix")["close"]
    days = spy.index.normalize().unique()
    print(f"SPY 5-min bars: {len(spy):,} over {len(days)} days ({days[0].date()} .. {days[-1].date()})")
    print(f"Costs: ${a.slippage:.2f}/share per side, ${a.commission:.2f}/contract per side. Per contract.\n")
    print(f"{'variant':42} {'trades':>6} {'/day':>5} {'win%':>6} {'avg $':>7} {'PF':>5} {'total $':>9} {'max DD $':>9}")
    res = {}
    for v in VARIANTS:
        t = simulate(spy, vix, v, a.slippage, a.commission)
        res[v.name] = t
        s = stats(t)
        print(f"{v.name:42} {s['n']:6d} {s['n'] / len(days):5.1f} {s['win']:6.1f} {s['avg']:7.2f} {s['pf']:5.2f} "
              f"{s['total']:9.0f} {s['dd']:9.0f}")
    t = res[VARIANTS[0].name]
    print("\nAs specified, by half-year:")
    half = t["exit"].dt.year.astype(str) + np.where(t["exit"].dt.month <= 6, "-H1", "-H2")
    for k, g in t.groupby(half):
        s = stats(g)
        print(f"  {k}: {s['n']:4d} trades  PF {s['pf']:4.2f}  {s['total']:8.0f}")
    hold = (t["exit"] - t["entry"]).dt.total_seconds() / 60
    print(f"\nHeld: median {hold.median():.0f} min; exits {t['reason'].value_counts().to_dict()}")


if __name__ == "__main__":
    main()
