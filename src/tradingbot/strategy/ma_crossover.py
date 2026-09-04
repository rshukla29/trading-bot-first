from __future__ import annotations

import pandas as pd

from tradingbot.indicators import avg_volume, rolling_vwap, rsi
from tradingbot.strategy.base import Signal, Strategy


class MovingAverageCrossoverStrategy(Strategy):
    """Fast/slow SMA crossover with trend, volume, RSI and VWAP confirmation.

    Entry (BUY) requires every enabled condition to agree:
      - golden cross: fast SMA crosses above slow SMA on the latest bar
      - trend filter: price above the trend_ma SMA (bull regime)
      - volume: recent participation is holding up — the short-window
        average volume is at least volume_multiplier x the long-window
        average. This is deliberately a *trend* check rather than a
        single-bar spike: an SMA crossover is a lagging signal, so the
        high-volume bar that drove the move is already days behind it,
        and demanding a spike on the crossover bar itself rejects most
        otherwise-valid signals. Spike confirmation belongs on breakout
        bars — see ConsolidationRetestStrategy, which does exactly that.
      - RSI: below rsi_overbought, so an already-extended move isn't chased
      - VWAP: price above the rolling VWAP (volume-weighted bullish side)

    Exit (EXIT) fires on either:
      - death cross: fast SMA crosses back below slow SMA
      - RSI exhaustion: RSI drops back through rsi_exit_overbought after
        being above it (momentum blow-off), a take-profit style exit

    Exits are never filtered by volume/VWAP — blocking an exit because
    volume looks weak would trap losing positions. Only entries are gated.
    Pass any filter's parameter as None to disable it.
    """

    def __init__(
        self,
        fast: int = 20,
        slow: int = 50,
        trend_ma: int | None = 200,
        volume_ma: int | None = 20,
        volume_fast_ma: int = 5,
        volume_multiplier: float = 1.0,
        rsi_period: int | None = 14,
        rsi_overbought: float = 70.0,
        rsi_exit_overbought: float = 80.0,
        vwap_window: int | None = 20,
        vwap_retest_tolerance: float = 0.015,
    ):
        if fast >= slow:
            raise ValueError("fast window must be shorter than slow window")
        self.fast = fast
        self.slow = slow
        self.trend_ma = trend_ma
        self.volume_ma = volume_ma
        self.volume_fast_ma = volume_fast_ma
        self.volume_multiplier = volume_multiplier
        self.rsi_period = rsi_period
        self.rsi_overbought = rsi_overbought
        self.rsi_exit_overbought = rsi_exit_overbought
        self.vwap_window = vwap_window
        self.vwap_retest_tolerance = vwap_retest_tolerance

    @property
    def min_history(self) -> int:
        windows = [self.slow, self.trend_ma, self.volume_ma, self.vwap_window]
        if self.rsi_period is not None:
            windows.append(self.rsi_period + 1)
        return max(w for w in windows if w is not None) + 1

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        latest = bars.iloc[-1]
        price = float(latest["close"])
        timestamp = latest.name

        if len(bars) < self.min_history:
            return Signal(symbol, "HOLD", price, timestamp, "insufficient history")

        closes = bars["close"]
        fast_sma = closes.rolling(self.fast).mean()
        slow_sma = closes.rolling(self.slow).mean()

        prev_diff = fast_sma.iloc[-2] - slow_sma.iloc[-2]
        curr_diff = fast_sma.iloc[-1] - slow_sma.iloc[-1]

        rsi_series = rsi(closes, self.rsi_period) if self.rsi_period else None

        # --- exits first: never filtered ---
        if prev_diff >= 0 and curr_diff < 0:
            return Signal(symbol, "EXIT", price, timestamp, f"death cross ({self.fast}/{self.slow} SMA)")

        if rsi_series is not None:
            prev_rsi, curr_rsi = rsi_series.iloc[-2], rsi_series.iloc[-1]
            if pd.notna(prev_rsi) and pd.notna(curr_rsi):
                if prev_rsi >= self.rsi_exit_overbought and curr_rsi < self.rsi_exit_overbought:
                    return Signal(
                        symbol, "EXIT", price, timestamp,
                        f"RSI exhaustion (fell back through {self.rsi_exit_overbought:.0f})",
                    )

        # --- entries: every enabled filter must agree ---
        if not (prev_diff <= 0 and curr_diff > 0):
            return Signal(symbol, "HOLD", price, timestamp, "no crossover")

        confirmations = [f"golden cross ({self.fast}/{self.slow} SMA)"]

        if self.trend_ma is not None:
            trend_sma = closes.rolling(self.trend_ma).mean().iloc[-1]
            if price <= trend_sma:
                return Signal(
                    symbol, "HOLD", price, timestamp,
                    f"golden cross blocked: price below {self.trend_ma}-SMA trend filter",
                )
            confirmations.append(f"above {self.trend_ma}-SMA")

        if self.volume_ma is not None:
            recent_volume = avg_volume(bars["volume"], self.volume_fast_ma).iloc[-1]
            baseline_volume = avg_volume(bars["volume"], self.volume_ma).iloc[-1]
            required = baseline_volume * self.volume_multiplier
            if pd.isna(baseline_volume) or pd.isna(recent_volume) or recent_volume < required:
                return Signal(
                    symbol, "HOLD", price, timestamp,
                    f"golden cross blocked: {self.volume_fast_ma}-day avg volume "
                    f"{recent_volume:,.0f} below {self.volume_multiplier}x the "
                    f"{self.volume_ma}-day average ({required:,.0f})",
                )
            confirmations.append(f"volume {recent_volume / baseline_volume:.1f}x baseline")

        if rsi_series is not None:
            curr_rsi = rsi_series.iloc[-1]
            if pd.notna(curr_rsi) and curr_rsi >= self.rsi_overbought:
                return Signal(
                    symbol, "HOLD", price, timestamp,
                    f"golden cross blocked: RSI {curr_rsi:.1f} overbought (>= {self.rsi_overbought:.0f})",
                )
            if pd.notna(curr_rsi):
                confirmations.append(f"RSI {curr_rsi:.1f}")

        if self.vwap_window is not None:
            vwap = rolling_vwap(bars, self.vwap_window).iloc[-1]
            if pd.isna(vwap) or price <= vwap:
                return Signal(
                    symbol, "HOLD", price, timestamp,
                    f"golden cross blocked: price below {self.vwap_window}-day VWAP",
                )
            # A pullback that tagged VWAP intraday and closed back above it
            # is the higher-quality "retest and hold" version of this entry.
            if latest["low"] <= vwap * (1 + self.vwap_retest_tolerance):
                confirmations.append("VWAP retest held")
            else:
                confirmations.append("above VWAP")

        return Signal(symbol, "BUY", price, timestamp, ", ".join(confirmations))
