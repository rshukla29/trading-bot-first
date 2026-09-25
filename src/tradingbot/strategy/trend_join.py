""""Trend Join Long" — intraday long-only gap-and-go strategy on S&P 500
names, from Humbled Trader's "AI trading bot with Claude + IBKR" article
(https://www.humbledtrader.com/blog/ai-trading-bot-claude-ibkr/).

Entry requires all six filters:
  D1  price above yesterday's daily high
  D2  yesterday's close above its 200-day SMA
  D3  today's open gapped up >= 3% from yesterday's close
  I1  price above today's premarket high
  I2  price above today's high so far (a new high-of-day breakout)
  I3  relative volume >= 2x the 14-day average

Exits (the article's rules.json):
  initial stop    low of day minus 1%
  +0.75R          sell one third
  +1.0R           stop to breakeven
  after breakeven trail the stop under 5-min swing lows (2 bars each side)
  15:51 ET        flatten everything

The article leaves several rules undefined; the choices made here:
  - Gap (D3) is today's regular-session open vs. yesterday's close.
  - I2 is checked on the last *completed* 5-min bar: its close must exceed
    the high of every earlier regular-session bar today. "Price" for D1/I1
    is that same close — acting on closed bars, not ticks, avoids entering
    on a wick that immediately reverses.
  - Relative volume (I3) is time-of-day adjusted: today's cumulative
    regular-session volume through the latest bar, divided by the average
    cumulative volume through the same time of day over the prior 14
    sessions. A plain "today so far vs. a full day's average" would be
    near-impossible to hit before the afternoon.
  - Premarket (I1) is 4:00-9:30 ET. No premarket prints = filter fails.
  - Low of day for the stop is the regular-session low (premarket
    excluded).
  - Swing low = a bar whose low is strictly below the lows of the 2 bars
    before and the 2 bars after it; the trailing stop goes 1 cent under
    the most recent one and only ever ratchets up.
  - The initial stop is additionally capped at 10% below entry (this
    account's standing max-stop rule); LOD-1% is almost always tighter.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, time

import pandas as pd

MARKET_OPEN = time(9, 30)


@dataclass(frozen=True)
class GapCandidate:
    symbol: str
    prev_close: float
    prev_high: float
    today_open: float
    gap_pct: float
    sma200: float


@dataclass(frozen=True)
class EntryCheck:
    ok: bool
    reason: str
    price: float = 0.0
    low_of_day: float = 0.0


def daily_prefilter(
    symbol: str, daily: pd.DataFrame, today: date, min_gap_pct: float, min_price: float, trend_ma: int = 200,
) -> GapCandidate | None:
    """D2 + D3 (+ minimum price) on daily bars whose last row is today's
    in-progress session. None if the symbol doesn't qualify."""
    if len(daily) < trend_ma + 1 or daily.index[-1].date() != today:
        return None
    sma = daily["close"].rolling(trend_ma).mean()
    prev, today_bar = daily.iloc[-2], daily.iloc[-1]
    prev_sma = float(sma.iloc[-2])
    if math.isnan(prev_sma) or prev["close"] <= prev_sma:
        return None
    if today_bar["open"] < min_price:
        return None
    gap_pct = (today_bar["open"] / prev["close"] - 1) * 100
    if gap_pct < min_gap_pct:
        return None
    return GapCandidate(
        symbol=symbol,
        prev_close=float(prev["close"]),
        prev_high=float(prev["high"]),
        today_open=float(today_bar["open"]),
        gap_pct=round(float(gap_pct), 2),
        sma200=round(prev_sma, 2),
    )


def rank_candidates(candidates: list[GapCandidate], max_candidates: int) -> list[GapCandidate]:
    """Biggest gaps first, capped — same ranking as the article."""
    return sorted(candidates, key=lambda c: c.gap_pct, reverse=True)[:max_candidates]


def completed_bars(bars: pd.DataFrame, now: pd.Timestamp, bar_minutes: int) -> pd.DataFrame:
    """IBKR returns the still-forming current bar as the last row; drop any
    bar that hasn't closed yet as of `now`."""
    return bars[bars.index + pd.Timedelta(minutes=bar_minutes) <= now]


def split_session(bars: pd.DataFrame, day: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(premarket, regular-session) bars for `day` from an ET-indexed
    frame that includes extended hours."""
    day_bars = bars[bars.index.date == day]
    times = day_bars.index.time
    premarket = day_bars[times < MARKET_OPEN]
    regular = day_bars[(times >= MARKET_OPEN) & (times < time(16, 0))]
    return premarket, regular


def relative_volume(today_rth: pd.DataFrame, history_rth: pd.DataFrame, lookback_days: int) -> float | None:
    """Time-of-day-adjusted relative volume (see module docstring). Returns
    None if there isn't enough history to compare against."""
    if today_rth.empty:
        return None
    cutoff = today_rth.index[-1].time()
    today_cum = float(today_rth["volume"].sum())

    past = history_rth[history_rth.index.date < today_rth.index[-1].date()]
    past = past[past.index.time <= cutoff]
    per_day = past.groupby(past.index.date)["volume"].sum()
    per_day = per_day.iloc[-lookback_days:]
    if len(per_day) < lookback_days or per_day.mean() <= 0:
        return None
    return today_cum / float(per_day.mean())


def check_entry(
    candidate: GapCandidate, premarket: pd.DataFrame, regular: pd.DataFrame, rvol: float | None, min_rvol: float,
) -> EntryCheck:
    """D1, I1, I2, I3 on completed bars (D2/D3 were checked in the prefilter)."""
    if len(regular) < 2:
        return EntryCheck(False, "not enough regular-session bars yet")
    if premarket.empty:
        return EntryCheck(False, "no premarket prints (I1 can't be checked)")

    price = float(regular["close"].iloc[-1])
    prior_hod = float(regular["high"].iloc[:-1].max())
    pm_high = float(premarket["high"].max())
    lod = float(regular["low"].min())

    if price <= candidate.prev_high:
        return EntryCheck(False, f"D1 fail: {price:.2f} <= prior-day high {candidate.prev_high:.2f}")
    if price <= pm_high:
        return EntryCheck(False, f"I1 fail: {price:.2f} <= premarket high {pm_high:.2f}")
    if price <= prior_hod:
        return EntryCheck(False, f"I2 fail: {price:.2f} <= high of day {prior_hod:.2f}")
    if rvol is None:
        return EntryCheck(False, "I3 fail: insufficient volume history")
    if rvol < min_rvol:
        return EntryCheck(False, f"I3 fail: rvol {rvol:.2f} < {min_rvol}")
    return EntryCheck(
        True,
        f"gap {candidate.gap_pct:.1f}%, close {price:.2f} > HOD {prior_hod:.2f} / PMH {pm_high:.2f} / "
        f"PDH {candidate.prev_high:.2f}, rvol {rvol:.2f}",
        price=price,
        low_of_day=lod,
    )


def initial_stop(entry: float, low_of_day: float, lod_buffer_pct: float, max_stop_pct: float) -> float:
    return round(max(low_of_day * (1 - lod_buffer_pct), entry * (1 - max_stop_pct)), 2)


def share_quantity(equity: float, risk_pct: float, entry: float, stop: float, max_position_value: float) -> int:
    """size = min(floor(risk_dollars / R), floor(max_position_value / price))."""
    r = entry - stop
    if r <= 0 or entry <= 0:
        return 0
    return max(0, min(math.floor(equity * risk_pct / r), math.floor(max_position_value / entry)))


def latest_swing_low(bars: pd.DataFrame, left: int = 2, right: int = 2) -> float | None:
    """Most recent confirmed swing low (needs `right` bars after it)."""
    lows = bars["low"].to_numpy()
    for i in range(len(lows) - 1 - right, left - 1, -1):
        neighbors = list(lows[i - left:i]) + list(lows[i + 1:i + 1 + right])
        if all(lows[i] < n for n in neighbors):
            return float(lows[i])
    return None


@dataclass
class PositionState:
    entry: float
    initial_stop: float
    stop: float
    qty: int
    remaining: int
    partial_taken: bool = False
    breakeven: bool = False

    @property
    def r(self) -> float:
        return self.entry - self.initial_stop


@dataclass(frozen=True)
class ExitPlan:
    flatten_reason: str | None = None
    partial_triggered: bool = False  # +partial_r reached this tick (partial_qty may be 0 on tiny positions)
    partial_qty: int = 0
    breakeven_reached: bool = False
    new_stop: float | None = None  # only set when the stop should move up


def plan_exit(
    pos: PositionState, price: float, now_time: time, swing_low: float | None,
    flatten_time: time, partial_r: float, partial_fraction: float, breakeven_r: float,
) -> ExitPlan:
    """Pure exit decision for one monitoring tick. Priority: EOD flatten >
    stop breached (safety net — the broker-side stop normally fills first)
    > partial > breakeven > trail."""
    if now_time >= flatten_time:
        return ExitPlan(flatten_reason="eod_flatten")
    if price <= pos.stop:
        return ExitPlan(flatten_reason="stop_hit")

    r_mult = (price - pos.entry) / pos.r if pos.r > 0 else 0.0
    partial_triggered = not pos.partial_taken and r_mult >= partial_r
    partial_qty = 0
    if partial_triggered:
        # Always leave at least one share running for the trail.
        partial_qty = max(0, min(math.floor(pos.qty * partial_fraction), pos.remaining - 1))

    new_stop = pos.stop
    breakeven_reached = not pos.breakeven and r_mult >= breakeven_r
    if pos.breakeven or breakeven_reached:
        new_stop = max(new_stop, pos.entry)
        if swing_low is not None and swing_low < price:
            new_stop = max(new_stop, round(swing_low - 0.01, 2))
    return ExitPlan(
        partial_triggered=partial_triggered,
        partial_qty=partial_qty,
        breakeven_reached=breakeven_reached,
        new_stop=new_stop if new_stop > pos.stop else None,
    )
