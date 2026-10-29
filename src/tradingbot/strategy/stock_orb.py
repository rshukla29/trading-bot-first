"""Stock opening-range breakout ("Stock ORB", strategy #7) — the rules of
Jake Schincariol's day-trader-skill pack (MIT,
https://github.com/Jakeschincariol/day-trader-skill), ported from its
trader-entry / trader-exit / trader-risk / trader-timemachine scripts.
Pure functions shared by the backtest (scripts/stock_orb_backtest.py) and
the live bot (run_stock_orb_bot.py).

Rules (defaults from the pack):
  - Opening range: the first 15 minutes (9:30, 9:35, 9:40 5-min bars); its
    high is the highest high, its low the lowest low. Every range bar must
    be present.
  - Entry: the first completed 5-min bar after the range that CLOSES above
    the range high ("a wick above the high is not a trigger"), ending inside
    the entry window 9:45-15:30. Long only.
  - Stop: the range midpoint, rounded down to the cent. Refused when the
    risk is under 0.05% of the price (inside the spread's noise).
  - Target: entry + target_r x risk (the pack defaults to 2R; its README
    example uses 1R — the backtest picks).
  - Exit: stop or target, whichever comes first (a bar that touches both
    counts as the stop — conservative), else flat by 15:55.
  - Size: shares = max loss per trade / risk, capped by the max order value.

Time Machine: the pack's own trader-timemachine, vendored verbatim in
tradingbot/vendor/timemachine.py (see run_time_machine below). From the
stock's last 60 days of 5-min bars it runs 10,000 simulated rest-of-day
paths plus the same rule on every past session, and prints one verdict.
Report-only in this repo: logged and journaled with each trade, never used
to block one.
"""
from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass
from datetime import time

import pandas as pd

MIN_RISK_PCT = 0.05


def _cents_down(x: float) -> float:
    return math.floor(x * 100 + 1e-9) / 100


def _cents(x: float) -> float:
    return round(x + 1e-12, 2)


@dataclass
class Trigger:
    ts: pd.Timestamp          # bar START time of the trigger bar
    entry: float              # trigger bar's close
    or_high: float
    or_low: float
    dollar_volume: float      # close x volume summed through the trigger bar


@dataclass
class Plan:
    entry: float
    stop: float
    target: float
    risk: float


def regular_session(bars: pd.DataFrame) -> pd.DataFrame:
    t = bars.index.time
    return bars[(t >= time(9, 30)) & (t < time(16, 0))]


def opening_range(day: pd.DataFrame, or_minutes: int = 15, bar_minutes: int = 5) -> tuple[float, float] | None:
    """(high, low) of the opening range, or None unless every range bar is present."""
    n = or_minutes // bar_minutes
    start = day.index[0].normalize() + pd.Timedelta(hours=9, minutes=30)
    expected = [start + pd.Timedelta(minutes=bar_minutes * i) for i in range(n)]
    rng = day.loc[day.index.isin(expected)]
    if len(rng) != n:
        return None
    return float(rng["high"].max()), float(rng["low"].min())


def find_trigger(day: pd.DataFrame, or_minutes: int = 15, bar_minutes: int = 5,
                 first_entry: time = time(9, 45), last_entry: time = time(15, 30)) -> Trigger | None:
    """First completed bar after the range whose close is above the range
    high and whose END time is inside the entry window. `day` holds one
    session's completed regular-hours bars."""
    if day.empty:
        return None
    rng = opening_range(day, or_minutes, bar_minutes)
    if rng is None:
        return None
    hi, lo = rng
    range_end = day.index[0].normalize() + pd.Timedelta(hours=9, minutes=30 + or_minutes)
    dollar_vol = 0.0
    for ts, b in day.iterrows():
        dollar_vol += float(b["close"]) * float(b["volume"])
        if ts < range_end:
            continue
        end = (ts + pd.Timedelta(minutes=bar_minutes)).time()
        if end < first_entry or end > last_entry:
            continue
        if b["close"] > hi:
            return Trigger(ts, float(b["close"]), hi, lo, dollar_vol)
    return None


def plan_trade(entry: float, or_high: float, or_low: float, target_r: float) -> Plan | None:
    """Stop at the range midpoint (rounded down), target at target_r x risk.
    None when the stop isn't below the entry or the risk is inside the spread."""
    stop = _cents_down((or_high + or_low) / 2)
    risk = entry - stop
    if risk <= 0 or risk / entry * 100 < MIN_RISK_PCT:
        return None
    return Plan(entry, stop, _cents(entry + target_r * risk), risk)


def share_quantity(entry: float, stop: float, max_loss: float, max_order_value: float) -> int:
    risk = entry - stop
    if risk <= 0 or entry <= 0:
        return 0
    return max(0, min(int(max_loss // risk), int(max_order_value // entry)))


def simulate_exit(after: pd.DataFrame, plan: Plan, flat_by: time = time(15, 55),
                  bar_minutes: int = 5) -> tuple[str, float, pd.Timestamp | None]:
    """Walk the bars after the entry bar: stop first, then target, else out
    at the close of the bar ending at flat_by. Returns (outcome, price, ts)."""
    last_close, last_ts = plan.entry, None
    for ts, b in after.iterrows():
        if b["low"] <= plan.stop:
            # A gap through the stop fills at the open, not the stop.
            return "stop", min(plan.stop, float(b["open"])), ts
        if b["high"] >= plan.target:
            # A limit sell fills at the target, or better on a gap above it.
            return "target", max(plan.target, float(b["open"])), ts
        last_close, last_ts = float(b["close"]), ts
        if (ts + pd.Timedelta(minutes=bar_minutes)).time() >= flat_by:
            break
    return "flat", last_close, last_ts


# --------------------------------------------------------------------------
# Time Machine (report-only) — the pack's own code, vendor/timemachine.py
# --------------------------------------------------------------------------

def time_machine_plan(symbol: str, trigger: Trigger, plan: Plan, flat_by: str = "15:55",
                      bar_minutes: int = 5) -> dict:
    """A Stock ORB trade in trader-exit's plan.json shape, for the vendored
    Time Machine. `symbol` in Yahoo notation (BRK-B). The entry bar is the
    trigger bar; entry_time is its close."""
    close = trigger.ts + pd.Timedelta(minutes=bar_minutes)
    return {"symbol": symbol, "side": "buy", "date": trigger.ts.date().isoformat(), "setup": "orb",
            "entry": plan.entry, "entry_time": close.strftime("%I:%M %p").lstrip("0"), "interval": "5m",
            "opening_range": {"high": trigger.or_high, "low": trigger.or_low, "minutes": 15},
            "stop": plan.stop, "stop_rule": "or_mid", "target": plan.target, "flat_by": flat_by}


def run_time_machine(plan_raw: dict, chart_text: str | None = None, now_utc: _dt.datetime | None = None,
                     until: str | None = "15:55") -> dict:
    """The vendored Time Machine on one plan -> its timemachine.json document
    (odds, history, verdict). Fetches Yahoo's 60-day 5-min chart unless
    `chart_text` is given. `until` defaults to the 15:55 flat-by, as the
    pack's SKILL.md says to. Raises vendor.timemachine.TMError when the run
    is refused (fewer than 20 full sessions, entry bar not in the data, ...)."""
    from tradingbot.vendor import timemachine as tm

    plan = tm.normalize_plan(plan_raw)
    now = now_utc or tm.parse_now(None)
    text = chart_text if chart_text is not None else tm.fetch_chart(plan["symbol"])
    return tm.time_machine(text, plan, now, until=until)


def time_machine_summary(doc: dict) -> str:
    """One line for the log and the journal: the odds, the history, and the
    verdict exactly as the Time Machine prints it."""
    m, h = doc["mc"], doc["history"]
    hist = (f"history {h['triggered']} of {h['sessions']} sessions fired: {h['target']} target / {h['stop']} stop "
            f"/ {h['close']} neither, avg {h['avg_R']:+.3f}R" if h.get("triggered") else "history: never fired")
    return (f"Time Machine (seed {m['seed']}, {m['replays']:,} replays to {m['until']}): target first "
            f"{m['p_target'] * 100:.1f}%, stop first {m['p_stop'] * 100:.1f}%, neither {m['p_neither'] * 100:.1f}%, "
            f"avg {m['avg_R']:+.3f}R; {hist}. {' '.join([doc['verdict']] + list(doc.get('verdict_notes') or []))}")
