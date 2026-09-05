from __future__ import annotations

import pandas as pd

from tradingbot.indicators import fib_retracement_fraction, rsi
from tradingbot.strategy.base import Signal, Strategy


class RSI2MeanReversionStrategy(Strategy):
    """Larry Connors' RSI(2) mean-reversion pullback (Connors Research,
    "High Probability ETF Trading", 2009), the standard, widely-published
    counterpart to trend-following crossover systems — genuinely a
    different strategy family (mean-reversion vs. trend-following), not a
    variant of MovingAverageCrossoverStrategy.

    Entry: price above the trend_ma SMA (only buy dips within an uptrend —
    this is what stops the strategy from "catching falling knives" in a
    genuine downtrend) AND RSI(2) drops below buy_threshold (default 5) —
    a 2-period RSI that low means an extreme, likely-to-snap-back short-term
    oversold move. Optionally also requires the pullback to sit in the
    classic Fibonacci "golden zone" (38.2%-61.8% retracement of the
    fib_lookback-window swing) — confirms the RSI dip is landing at a
    price level with some structural support behind it, not an arbitrary
    one. Off by default (fib_lookback=None); a genuine second, independent
    confirmation, not a variant of the RSI condition, so stacking it will
    reduce trade frequency — check with signal_diag-style attribution
    before assuming it improves results.

    Exit: RSI(2) rises back above sell_threshold (default 65), regardless
    of the trend filter or the fib zone — exits are never filtered,
    matching every other strategy in this codebase.

    Published backtests report >75% win rates in equities with this setup,
    because it's a short-term reversion bet with a high hit rate and small
    average gains — the opposite payoff shape from the long-options ORB
    strategy (low win rate, large average win). The stop-loss configured
    on the surrounding bot (see risk.py / run_ibkr_bot.py) is what caps the
    rare trade that doesn't revert, since the classic Connors rules have no
    hard stop of their own.
    """

    def __init__(
        self,
        trend_ma: int = 200,
        rsi_period: int = 2,
        buy_threshold: float = 5.0,
        sell_threshold: float = 65.0,
        fib_lookback: int | None = None,
        fib_zone: tuple[float, float] = (0.382, 0.618),
    ):
        self.trend_ma = trend_ma
        self.rsi_period = rsi_period
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.fib_lookback = fib_lookback
        self.fib_zone = fib_zone

    @property
    def min_history(self) -> int:
        windows = [self.trend_ma, self.rsi_period + 1]
        if self.fib_lookback is not None:
            windows.append(self.fib_lookback)
        return max(windows) + 1

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        latest = bars.iloc[-1]
        price = float(latest["close"])
        timestamp = latest.name

        if len(bars) < self.min_history:
            return Signal(symbol, "HOLD", price, timestamp, "insufficient history")

        closes = bars["close"]
        rsi_series = rsi(closes, self.rsi_period)
        curr_rsi = rsi_series.iloc[-1]
        if pd.isna(curr_rsi):
            return Signal(symbol, "HOLD", price, timestamp, "RSI not yet available")

        if curr_rsi > self.sell_threshold:
            return Signal(symbol, "EXIT", price, timestamp, f"RSI({self.rsi_period})={curr_rsi:.1f} above {self.sell_threshold:.0f}")

        trend_sma = closes.rolling(self.trend_ma).mean().iloc[-1]
        if price <= trend_sma:
            return Signal(
                symbol, "HOLD", price, timestamp,
                f"below {self.trend_ma}-SMA trend filter (RSI={curr_rsi:.1f})",
            )

        if curr_rsi >= self.buy_threshold:
            return Signal(symbol, "HOLD", price, timestamp, f"RSI({self.rsi_period})={curr_rsi:.1f}, no setup")

        reason = f"RSI({self.rsi_period})={curr_rsi:.1f} below {self.buy_threshold:.0f}, above {self.trend_ma}-SMA trend"

        if self.fib_lookback is not None:
            fraction = fib_retracement_fraction(bars, self.fib_lookback).iloc[-1]
            low, high = self.fib_zone
            if pd.isna(fraction) or not (low <= fraction <= high):
                return Signal(
                    symbol, "HOLD", price, timestamp,
                    f"RSI({self.rsi_period})={curr_rsi:.1f} oversold blocked: retracement "
                    f"{fraction:.2f} outside golden zone [{low}-{high}]" if pd.notna(fraction) else
                    f"RSI({self.rsi_period})={curr_rsi:.1f} oversold blocked: retracement undefined",
                )
            reason += f", {fraction * 100:.0f}% fib retracement"

        return Signal(symbol, "BUY", price, timestamp, reason)
