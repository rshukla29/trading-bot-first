from __future__ import annotations

import pandas as pd

from tradingbot.strategy.base import Signal, Strategy


class MovingAverageCrossoverStrategy(Strategy):
    """Classic fast/slow SMA crossover.

    BUY when the fast SMA crosses above the slow SMA on the latest bar
    (golden cross). EXIT when the fast SMA crosses back below the slow SMA
    (death cross) — this is a trend-loss exit, separate from and in addition
    to the hard stop-loss placed on the broker side at entry time.
    """

    def __init__(self, fast: int = 20, slow: int = 50):
        if fast >= slow:
            raise ValueError("fast window must be shorter than slow window")
        self.fast = fast
        self.slow = slow

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        if len(bars) < self.slow + 1:
            latest = bars.iloc[-1]
            return Signal(symbol, "HOLD", float(latest["close"]), latest.name, "insufficient history")

        fast_sma = bars["close"].rolling(self.fast).mean()
        slow_sma = bars["close"].rolling(self.slow).mean()

        prev_diff = fast_sma.iloc[-2] - slow_sma.iloc[-2]
        curr_diff = fast_sma.iloc[-1] - slow_sma.iloc[-1]

        latest = bars.iloc[-1]
        price = float(latest["close"])
        timestamp = latest.name

        if prev_diff <= 0 and curr_diff > 0:
            return Signal(symbol, "BUY", price, timestamp, f"golden cross ({self.fast}/{self.slow} SMA)")
        if prev_diff >= 0 and curr_diff < 0:
            return Signal(symbol, "EXIT", price, timestamp, f"death cross ({self.fast}/{self.slow} SMA)")
        return Signal(symbol, "HOLD", price, timestamp, "no crossover")
