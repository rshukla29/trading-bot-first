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
