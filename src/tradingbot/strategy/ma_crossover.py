from __future__ import annotations

import pandas as pd

from tradingbot.strategy.base import Signal, Strategy


class MovingAverageCrossoverStrategy(Strategy):
    """Fast/slow SMA crossover with an optional long-term trend filter.

    BUY when the fast SMA crosses above the slow SMA on the latest bar
    (golden cross) *and* price is above the trend_ma SMA (bull regime) —
    the trend filter exists because a bare crossover keeps buying dips
    inside downtrends and getting stopped out; requiring the long-term
    trend to already be up filters most of those losing entries.

    EXIT when the fast SMA crosses back below the slow SMA (death cross),
    regardless of the trend filter — exits should never be filtered, only
    entries. This is in addition to the hard stop-loss placed on the
    broker side at entry time.

    Pass trend_ma=None to disable the filter and get a bare crossover.
    """

    def __init__(self, fast: int = 20, slow: int = 50, trend_ma: int | None = 200):
        if fast >= slow:
            raise ValueError("fast window must be shorter than slow window")
        self.fast = fast
        self.slow = slow
        self.trend_ma = trend_ma

    def _min_bars(self) -> int:
        required = self.slow
        if self.trend_ma is not None:
            required = max(required, self.trend_ma)
        return required + 1

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        if len(bars) < self._min_bars():
            latest = bars.iloc[-1]
            return Signal(symbol, "HOLD", float(latest["close"]), latest.name, "insufficient history")

        fast_sma = bars["close"].rolling(self.fast).mean()
        slow_sma = bars["close"].rolling(self.slow).mean()

        prev_diff = fast_sma.iloc[-2] - slow_sma.iloc[-2]
        curr_diff = fast_sma.iloc[-1] - slow_sma.iloc[-1]

        latest = bars.iloc[-1]
        price = float(latest["close"])
        timestamp = latest.name

        golden_cross = prev_diff <= 0 and curr_diff > 0
        death_cross = prev_diff >= 0 and curr_diff < 0

        if golden_cross:
            if self.trend_ma is None:
                return Signal(symbol, "BUY", price, timestamp, f"golden cross ({self.fast}/{self.slow} SMA)")
            trend_sma = bars["close"].rolling(self.trend_ma).mean().iloc[-1]
            if price > trend_sma:
                return Signal(
                    symbol, "BUY", price, timestamp,
                    f"golden cross ({self.fast}/{self.slow} SMA) above {self.trend_ma}-SMA trend filter",
                )
            return Signal(
                symbol, "HOLD", price, timestamp,
                f"golden cross blocked: price below {self.trend_ma}-SMA trend filter",
            )

        if death_cross:
            return Signal(symbol, "EXIT", price, timestamp, f"death cross ({self.fast}/{self.slow} SMA)")

        return Signal(symbol, "HOLD", price, timestamp, "no crossover")
