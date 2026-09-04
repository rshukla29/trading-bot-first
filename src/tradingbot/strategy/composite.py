from __future__ import annotations

import pandas as pd

from tradingbot.strategy.base import Signal, Strategy


class CompositeStrategy(Strategy):
    """Runs a primary strategy plus additional entry-only strategies.

    The primary strategy owns exits and gets first say on entries. The
    extra strategies can only add BUY signals — they never override an
    EXIT, so a position flagged for exit is always exited regardless of
    what another setup thinks.
    """

    def __init__(self, primary: Strategy, extra_entries: list[Strategy] | None = None):
        self.primary = primary
        self.extra_entries = extra_entries or []

    @property
    def min_history(self) -> int:
        return max([self.primary.min_history] + [s.min_history for s in self.extra_entries])

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        primary_signal = self.primary.generate_signal(symbol, bars)
        if primary_signal.action in ("BUY", "EXIT"):
            return primary_signal

        for strategy in self.extra_entries:
            signal = strategy.generate_signal(symbol, bars)
            if signal.action == "BUY":
                return signal

        return primary_signal
