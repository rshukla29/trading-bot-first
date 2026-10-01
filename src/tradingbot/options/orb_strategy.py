from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

OptionAction = Literal["BUY_CALL", "BUY_PUT", "HOLD"]


@dataclass(frozen=True)
class OptionSignal:
    symbol: str
    action: OptionAction
    underlying_price: float
    timestamp: pd.Timestamp
    reason: str


class OpeningRangeBreakoutStrategy:
    """Classic intraday setup: the high/low of the first `opening_range_minutes`
    of the session defines a range; the first bar to close outside it in
    either direction is the breakout entry — a call on an upside break, a
    put on a downside break. Fires at most once per direction per day (a
    later bar that's still outside the range isn't a fresh breakout).

    Works on intraday bars for one symbol only. `bars` must be indexed by
    timezone-aware timestamps and may span multiple trading days — only the
    latest bar's calendar day is used to find "today's" session.
    """

    def __init__(self, opening_range_minutes: int = 15, bar_minutes: int = 5):
        if opening_range_minutes % bar_minutes != 0:
            raise ValueError("opening_range_minutes must be a multiple of bar_minutes")
        self.opening_range_minutes = opening_range_minutes
        self.bar_minutes = bar_minutes
        self._range_bar_count = opening_range_minutes // bar_minutes

    @property
    def min_history(self) -> int:
        return self._range_bar_count + 1

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> OptionSignal:
        latest = bars.iloc[-1]
        price = float(latest["close"])
        timestamp = latest.name

        today = bars[bars.index.date == timestamp.date()]
        if len(today) <= self._range_bar_count:
            return OptionSignal(symbol, "HOLD", price, timestamp, "opening range still forming")

        range_bars = today.iloc[: self._range_bar_count]
        range_high = float(range_bars["high"].max())
        range_low = float(range_bars["low"].min())

        post_range = today.iloc[self._range_bar_count :]
        prior = post_range.iloc[:-1]

        already_broke_up = bool((prior["close"] > range_high).any())
        already_broke_down = bool((prior["close"] < range_low).any())

        if price > range_high and not already_broke_up:
            return OptionSignal(
                symbol, "BUY_CALL", price, timestamp,
                f"opening range breakout up (range {range_low:.2f}-{range_high:.2f})",
            )
        if price < range_low and not already_broke_down:
            return OptionSignal(
                symbol, "BUY_PUT", price, timestamp,
                f"opening range breakdown (range {range_low:.2f}-{range_high:.2f})",
            )
        return OptionSignal(symbol, "HOLD", price, timestamp, "inside or already-broken range")

    def reentry_direction(self, bars: pd.DataFrame, entries_today: dict, max_reentries: int) -> str | None:
        """Re-entry after an exit when the trend resumes: 'C' if the latest
        completed bar closes at a new session high above the range and a
        call was already traded (and closed) today, 'P' likewise for a new
        session low below the range; at most `max_reentries` extra entries
        per direction per day. `bars` may span several days — only the
        latest bar's day is used. Shared by the backtest and the live bot."""
        if not max_reentries:
            return None
        day = bars.index[-1].date()
        today = bars[bars.index.date == day]
        day_range = self.range_for_day(today, day)
        if day_range is None or len(today) <= self._range_bar_count:
            return None
        range_low, range_high = day_range
        close = float(today["close"].iloc[-1])
        prior = today.iloc[:-1]
        if 1 <= entries_today.get("C", 0) <= max_reentries and close > range_high and close > float(prior["high"].max()):
            return "C"
        if 1 <= entries_today.get("P", 0) <= max_reentries and close < range_low and close < float(prior["low"].min()):
            return "P"
        return None

    def vwap_reentry_direction(
        self, bars: pd.DataFrame, vwap: float, entries_today: dict, max_reentries: int,
    ) -> str | None:
        """Re-entry on a good VWAP pullback: after a trade in a direction has
        closed, 'C' when the latest completed bar dips to VWAP (low <= VWAP)
        and closes back above it as an up bar; 'P' when it rallies to VWAP
        (high >= VWAP) and closes back below it as a down bar. At most
        `max_reentries` extra entries per direction per day."""
        if not max_reentries or vwap is None:
            return None
        bar = bars.iloc[-1]
        o, h, l, c = (float(bar[k]) for k in ("open", "high", "low", "close"))
        if 1 <= entries_today.get("C", 0) <= max_reentries and l <= vwap < c and c > o:
            return "C"
        if 1 <= entries_today.get("P", 0) <= max_reentries and h >= vwap > c and c < o:
            return "P"
        return None

    def confirmed_breakout(self, bars: pd.DataFrame, n: int, entries_today: dict) -> str | None:
        """'C'/'P' on the bar completing the n-th consecutive close beyond
        the range (the first such run per direction per day; n=2 is the
        practitioner "two closes outside" confirmation). `bars` may span
        several days — only the latest bar's day is used."""
        day = bars.index[-1].date()
        today = bars[bars.index.date == day]
        day_range = self.range_for_day(today, day)
        if day_range is None:
            return None
        lo, hi = day_range
        post = today["close"].iloc[self._range_bar_count:]
        if len(post) < n:
            return None
        last, before = post.iloc[-n:], (post.iloc[-n - 1] if len(post) > n else None)
        if entries_today.get("C", 0) == 0 and (last > hi).all() and (before is None or before <= hi):
            return "C"
        if entries_today.get("P", 0) == 0 and (last < lo).all() and (before is None or before >= lo):
            return "P"
        return None

    def passes_entry_filters(self, bars: pd.DataFrame, right: str, min_range_pct: float, vwap: float | None) -> bool:
        """Practitioner entry filters: the opening range must be at least
        min_range_pct of the day's open, and (when `vwap` is given) a call
        needs the close above session VWAP, a put below."""
        day = bars.index[-1].date()
        today = bars[bars.index.date == day]
        day_range = self.range_for_day(today, day)
        if day_range is None:
            return False
        lo, hi = day_range
        if min_range_pct and (hi - lo) < min_range_pct * float(today["open"].iloc[0]):
            return False
        if vwap is not None:
            close = float(today["close"].iloc[-1])
            if (right == "C" and close <= vwap) or (right == "P" and close >= vwap):
                return False
        return True

    def range_for_day(self, bars: pd.DataFrame, day) -> tuple[float, float] | None:
        """(range_low, range_high) for a given calendar day, or None if that
        day doesn't yet have enough bars. Used by the backtest to check the
        "underlying re-enters the opening range" exit without recomputing
        the range from scratch on every bar."""
        today = bars[bars.index.date == day]
        if len(today) < self._range_bar_count:
            return None
        range_bars = today.iloc[: self._range_bar_count]
        return float(range_bars["low"].min()), float(range_bars["high"].max())
