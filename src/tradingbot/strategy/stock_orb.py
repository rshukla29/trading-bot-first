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

Time Machine (trader-timemachine): from the stock's last ~60 days of
5-min bars, (1) replay every past session where this setup triggered and
(2) run 10,000 simulated rest-of-day paths built bar by bar from past bars
at about the same time of day. Report-only in this repo: it is logged and
journaled with each trade, never used to block one.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import time

import pandas as pd

SESSION_BARS = 78  # 9:30-16:00 in 5-min bars
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
# Time Machine (report-only)
# --------------------------------------------------------------------------

@dataclass
class TimeMachineReport:
    sessions: int
    mc_target_pct: float
    mc_stop_pct: float
    mc_avg_r: float
    hist_trades: int
    hist_target: int
    hist_stop: int
    hist_avg_r: float | None
    verdict: str

    def summary(self) -> str:
        hist = (f"history {self.hist_trades} trades ({self.hist_target} target / {self.hist_stop} stop), "
                f"avg {self.hist_avg_r:+.2f}R" if self.hist_avg_r is not None else "history: no past triggers")
        return (f"Time Machine ({self.sessions} sessions): simulated {self.mc_target_pct:.0f}% target-first / "
                f"{self.mc_stop_pct:.0f}% stop-first, avg {self.mc_avg_r:+.2f}R; {hist} — {self.verdict}")


def full_sessions(bars: pd.DataFrame, before: pd.Timestamp | None = None) -> list[pd.DataFrame]:
    """Complete 78-bar regular sessions (half days and gappy days dropped),
    strictly before `before` if given."""
    rth = regular_session(bars)
    out = []
    for d, day in rth.groupby(rth.index.date):
        if before is not None and pd.Timestamp(d) >= before.normalize().tz_localize(None):
            continue
        if len(day) == SESSION_BARS:
            out.append(day)
    return out


def time_machine(history: pd.DataFrame, plan: Plan, entry_slot: int, target_r: float,
                 or_minutes: int = 15, replays: int = 10_000, window: int = 2, seed: int = 19,
                 min_sessions: int = 20, today: pd.Timestamp | None = None) -> TimeMachineReport | None:
    """`entry_slot`: index (0-77) of the first bar AFTER the trigger bar.
    None when there are fewer than `min_sessions` full sessions."""
    sessions = full_sessions(history, today)
    if len(sessions) < min_sessions:
        return None

    # (1) Monte Carlo: ratio-to-previous-close candles pooled by time-of-day slot.
    pool: list[list[tuple[float, float, float, float]]] = [[] for _ in range(SESSION_BARS)]
    for day in sessions:
        o, h, l, c = (day[k].to_numpy() for k in ("open", "high", "low", "close"))
        for i in range(1, SESSION_BARS):
            pc = c[i - 1]
            if pc > 0:
                pool[i].append((o[i] / pc, h[i] / pc, l[i] / pc, c[i] / pc))
    rng = random.Random(seed)
    outcomes = {"target": 0, "stop": 0, "close": 0}
    total_r = 0.0
    for _ in range(replays):
        price = plan.entry
        result = None
        for i in range(max(1, entry_slot), SESSION_BARS):
            j = min(SESSION_BARS - 1, max(1, i + rng.randint(-window, window)))
            _, rh, rl, rc = rng.choice(pool[j])
            lo, hi, price_next = price * rl, price * rh, price * rc
            if lo <= plan.stop:
                result = "stop"
                break
            if hi >= plan.target:
                result = "target"
                break
            price = price_next
        if result == "stop":
            total_r -= 1.0
        elif result == "target":
            total_r += target_r
        else:
            result = "close"
            total_r += (price - plan.entry) / plan.risk
        outcomes[result] += 1

    # (2) History: replay this setup on every past full session.
    hist_r = []
    hist_outcomes = {"target": 0, "stop": 0, "flat": 0}
    for day in sessions:
        trig = find_trigger(day, or_minutes)
        if trig is None:
            continue
        p = plan_trade(trig.entry, trig.or_high, trig.or_low, target_r)
        if p is None:
            continue
        outcome, px, _ = simulate_exit(day[day.index > trig.ts], p, flat_by=time(16, 0))
        hist_outcomes[outcome] += 1
        hist_r.append(target_r if outcome == "target" else -1.0 if outcome == "stop" else (px - p.entry) / p.risk)

    one_sided = outcomes["target"] + outcomes["stop"]
    share = outcomes["target"] / one_sided * 100 if one_sided else 0.0
    breakeven = 100 / (1 + target_r)
    gap = share - breakeven
    verdict = ("coin flip" if abs(gap) < 5 else
               ("leans target" if gap > 0 else "leans stop") if abs(gap) < 15 else
               ("strongly leans target" if gap > 0 else "strongly leans stop"))
    return TimeMachineReport(
        sessions=len(sessions),
        mc_target_pct=outcomes["target"] / replays * 100,
        mc_stop_pct=outcomes["stop"] / replays * 100,
        mc_avg_r=total_r / replays,
        hist_trades=len(hist_r),
        hist_target=hist_outcomes["target"],
        hist_stop=hist_outcomes["stop"],
        hist_avg_r=sum(hist_r) / len(hist_r) if hist_r else None,
        verdict=verdict,
    )
