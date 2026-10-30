"""Backtest the Stock ORB Time Machine gate on the latest 60 days.

For every Stock ORB trigger (strategy/stock_orb.py rules) the vendored Time
Machine (vendor/timemachine.py, 10,000 replays, seed 19, to 15:55) is run
exactly as the live bot runs it before an entry. It only uses the sessions
before the trigger's date, so nothing from the trade day or later leaks in;
dates with fewer than 20 full sessions before them are left out (the Time
Machine refuses those).

Then two comparisons on the same days:
  - every trigger: all of them vs the ones the Time Machine agrees with
    (verdict leans / strongly toward the target) vs the ones it doesn't;
  - the live bot: the published rules (3 trades a day, $50 risk, $150 a day,
    $4,000 max, signals in time order, most dollars traded first) with and
    without the gate; gated, at most 10 candidates are checked per 5-min bar.

Costs as in scripts/stock_orb_backtest.py: $0.01/share slippage on market
fills, IBKR fixed commission both sides. One Yahoo request per symbol for its
60-day chart (the Time Machine's own fetch), cached in data/tm_charts.pkl.

    python scripts/stock_orb_tm_backtest.py             # uses the cache if there is one
    python scripts/stock_orb_tm_backtest.py --refresh   # re-download the charts
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

import pandas as pd

from tradingbot.sp500_scanner import fetch_sp500_constituents
from tradingbot.strategy.stock_orb import (Trigger, find_trigger, plan_trade, regular_session, run_time_machine,
                                           share_quantity, simulate_exit, time_machine_check, time_machine_plan)

CACHE = Path("data/tm_charts.pkl")
OUT = Path("data/stock_orb_tm_backtest.csv")
MAX_LOSS, MAX_ORDER, DAILY_LOSS, MAX_TRADES, MAX_CHECKS = 50.0, 4_000.0, 150.0, 3, 10
SLIP, TARGET_R, MIN_PRICE, MIN_SESSIONS = 0.01, 2.0, 5.0, 20


def commission(shares: int) -> float:
    return max(1.0, 0.005 * shares)


def load_charts(refresh: bool) -> dict[str, str]:
    if CACHE.exists() and not refresh:
        return pd.read_pickle(CACHE)
    from tradingbot.vendor import timemachine as tm
    charts = {}
    for n, sym in enumerate(fetch_sp500_constituents()["symbol"]):
        try:
            charts[sym] = tm.fetch_chart(sym)
        except tm.TMError as e:
            print(f"  {sym}: {e}")
        if n % 100 == 99:
            print(f"  fetched {n + 1}", flush=True)
    CACHE.parent.mkdir(exist_ok=True)
    pd.to_pickle(charts, CACHE)
    return charts


def bars_from_chart(text: str) -> pd.DataFrame:
    r = json.loads(text)["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    df = pd.DataFrame({k: q[k] for k in ("open", "high", "low", "close", "volume")},
                      index=pd.to_datetime(r["timestamp"], unit="s", utc=True).tz_convert("America/New_York"))
    return regular_session(df.dropna())


def triggers_for(sym: str, text: str) -> list[dict]:
    df = bars_from_chart(text)
    days = sorted(set(df.index.date))
    out = []
    for n, d in enumerate(days):
        day = df[df.index.date == d]
        if len(day) < 78 or n < MIN_SESSIONS or day["close"].iloc[0] < MIN_PRICE:
            continue  # half days, and dates the Time Machine would refuse
        trig = find_trigger(day)
        if trig is None:
            continue
        plan = plan_trade(trig.entry, trig.or_high, trig.or_low, TARGET_R)
        if plan is None:
            continue
        fill = trig.entry + SLIP
        qty = share_quantity(fill, plan.stop, MAX_LOSS, MAX_ORDER)
        if qty <= 0:
            continue
        outcome, px, _ = simulate_exit(day[day.index > trig.ts], replace(plan, entry=fill))
        exit_px = px if outcome == "target" else px - SLIP
        pnl = (exit_px - fill) * qty - 2 * commission(qty)
        out.append(dict(symbol=sym, date=d, ts=trig.ts, entry=trig.entry, or_high=trig.or_high, or_low=trig.or_low,
                        dollar_volume=trig.dollar_volume, stop=plan.stop, target=plan.target, qty=qty,
                        risk=qty * (fill - plan.stop), outcome=outcome, pnl=pnl, r=pnl / (qty * (fill - plan.stop)),
                        plan=plan))
    return out


def judge(task: tuple[str, str]) -> list[dict]:
    """All triggers of one symbol, each with the Time Machine's verdict."""
    from tradingbot.vendor import timemachine as tm
    sym, text = task
    rows = triggers_for(sym, text)
    for t in rows:
        trig = Trigger(t["ts"], t["entry"], t["or_high"], t["or_low"], t["dollar_volume"])
        try:
            check = time_machine_check(run_time_machine(time_machine_plan(sym, trig, t["plan"]), chart_text=text))
            t.update(agrees=bool(check["agrees"]), p_target=check["p_target"], p_stop=check["p_stop"],
                     verdict=check["verdict"].split(":")[0])
        except tm.TMError as e:
            t.update(agrees=False, p_target=None, p_stop=None, verdict=f"refused: {e}"[:60])
        del t["plan"]
    return rows


def live_bot(t: pd.DataFrame, gated: bool) -> pd.DataFrame:
    """The bot's own day: candidates per 5-min bar, most dollars first; gated,
    only the first 10 per bar get a Time Machine check."""
    kept = []
    for _, day in t.sort_values(["ts", "dollar_volume"], ascending=[True, False]).groupby("date"):
        taken, budget = 0, 0.0
        for _, bar in day.groupby("ts", sort=True):
            checks = 0
            for _, c in bar.iterrows():
                if taken >= MAX_TRADES:
                    break
                if budget + c.risk > DAILY_LOSS:
                    continue
                if gated:
                    if checks >= MAX_CHECKS:
                        break
                    checks += 1
                    if not c.agrees:
                        continue
                kept.append(c)
                taken += 1
                budget += c.risk
    return pd.DataFrame(kept)


def stats(t: pd.DataFrame) -> str:
    if t.empty:
        return "no trades"
    wins, losses = t[t.pnl > 0].pnl.sum(), -t[t.pnl <= 0].pnl.sum()
    eq = t.sort_values("ts").pnl.cumsum()
    return (f"{len(t):6d} trades  win {(t.pnl > 0).mean() * 100:4.1f}%  avg {t.r.mean():+.3f}R  "
            f"PF {wins / losses if losses else float('inf'):4.2f}  net ${t.pnl.sum():+9,.0f}  "
            f"max DD ${(eq - eq.cummax()).min():,.0f}  target/stop/flat "
            f"{(t.outcome == 'target').sum()}/{(t.outcome == 'stop').sum()}/{(t.outcome == 'flat').sum()}")


def main() -> None:
    charts = load_charts("--refresh" in sys.argv)
    print(f"{len(charts)} charts; running the Time Machine on every trigger...", flush=True)
    with ProcessPoolExecutor() as pool:
        rows = [r for rs in pool.map(judge, charts.items(), chunksize=4) for r in rs]
    t = pd.DataFrame(rows)
    t.to_csv(OUT, index=False)
    days = sorted(t.date.unique())
    mid = days[len(days) // 2]
    print(f"\n{len(t)} triggers on {len(days)} sessions {days[0]} .. {days[-1]} "
          f"(each with >= {MIN_SESSIONS} earlier sessions for the Time Machine)")
    print(f"Verdicts: {t.verdict.value_counts().to_dict()}\n")
    for label, part in (("all days", t), (f"first half (before {mid})", t[t.date < mid]),
                        (f"second half (from {mid})", t[t.date >= mid])):
        print(f"--- {label}")
        print(f"  every trigger           {stats(part)}")
        print(f"  Time Machine agrees     {stats(part[part.agrees])}")
        print(f"  Time Machine disagrees  {stats(part[~part.agrees])}")
        print(f"  live bot, no gate       {stats(live_bot(part, gated=False))}")
        print(f"  live bot, gated         {stats(live_bot(part, gated=True))}\n")
    print(f"Every trigger with its verdict: {OUT}")


if __name__ == "__main__":
    main()
