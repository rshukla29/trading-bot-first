from __future__ import annotations

import pandas as pd

from tradingbot.strategy.base import Signal, Strategy


class ConsolidationRetestStrategy(Strategy):
    """Breakout-from-consolidation followed by a successful retest.

    The setup, in order:
      1. Consolidation — a window of `consolidation_window` bars whose full
         range is tighter than `max_range_pct` (price coiling, not trending).
      2. Breakout — a later bar closes above that range's high, on at least
         `volume_multiplier` x the consolidation's average volume.
      3. Retest — a subsequent bar dips back to the breakout level (former
         resistance, now support) within `retest_tolerance`, but *closes
         back above it*, i.e. support held. That close is the entry.

    If price closed back below the breakout level at any point between the
    breakout and the retest, the setup is void — that's a failed breakout,
    not a retest.

    This strategy only emits BUY. It has no EXIT rule of its own: exits for
    positions it opens are handled by the broker-side stop-loss placed at
    entry, and by whatever exit rule the composite's primary strategy
    applies. Defining a robust discretionary "breakdown" exit is a separate
    problem, and the hard stop is the sanctioned risk control here.
    """

    def __init__(
        self,
        consolidation_window: int = 15,
        max_range_pct: float = 0.06,
        max_bars_since_breakout: int = 5,
        retest_tolerance: float = 0.02,
        volume_multiplier: float = 1.2,
    ):
        self.consolidation_window = consolidation_window
        self.max_range_pct = max_range_pct
        self.max_bars_since_breakout = max_bars_since_breakout
        self.retest_tolerance = retest_tolerance
        self.volume_multiplier = volume_multiplier

    @property
    def min_history(self) -> int:
        return self.consolidation_window + self.max_bars_since_breakout + 2

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        latest = bars.iloc[-1]
        price = float(latest["close"])
        timestamp = latest.name

        if len(bars) < self.min_history:
            return Signal(symbol, "HOLD", price, timestamp, "insufficient history")

        # Walk back through candidate breakout bars, most recent first.
        for offset in range(1, self.max_bars_since_breakout + 1):
            breakout_pos = len(bars) - 1 - offset
            window_start = breakout_pos - self.consolidation_window
            if window_start < 0:
                break

            window = bars.iloc[window_start:breakout_pos]
            range_high = float(window["high"].max())
            range_low = float(window["low"].min())
            if range_low <= 0:
                continue
            if (range_high - range_low) / range_low > self.max_range_pct:
                continue  # not a tight enough consolidation

            breakout_bar = bars.iloc[breakout_pos]
            if float(breakout_bar["close"]) <= range_high:
                continue  # no breakout on this bar

            mean_volume = float(window["volume"].mean())
            if mean_volume > 0 and float(breakout_bar["volume"]) < mean_volume * self.volume_multiplier:
                continue  # breakout lacked volume conviction

            # Between breakout and today, price must not have closed back
            # below the breakout level — that would be a failed breakout.
            between = bars.iloc[breakout_pos + 1 : len(bars) - 1]
            if (between["close"] < range_high).any():
                continue

            # Today: dipped to retest the breakout level but closed above it.
            retested = float(latest["low"]) <= range_high * (1 + self.retest_tolerance)
            held = price >= range_high
            if retested and held:
                return Signal(
                    symbol, "BUY", price, timestamp,
                    f"consolidation breakout ({self.consolidation_window}-bar range "
                    f"{range_low:.2f}-{range_high:.2f}) retested and held",
                )

        return Signal(symbol, "HOLD", price, timestamp, "no consolidation retest setup")
