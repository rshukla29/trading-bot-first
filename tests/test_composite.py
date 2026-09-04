from datetime import datetime

import pandas as pd

from tradingbot.strategy.base import Signal, Strategy
from tradingbot.strategy.composite import CompositeStrategy


class FakeStrategy(Strategy):
    def __init__(self, action: str, min_history: int = 10):
        self.action = action
        self._min_history = min_history

    @property
    def min_history(self) -> int:
        return self._min_history

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        return Signal(symbol, self.action, 100.0, datetime(2024, 1, 1), f"fake {self.action}")


BARS = pd.DataFrame({"close": [100.0]}, index=pd.date_range("2024-01-01", periods=1))


def test_primary_exit_wins_over_extra_entry():
    composite = CompositeStrategy(FakeStrategy("EXIT"), [FakeStrategy("BUY")])
    signal = composite.generate_signal("TEST", BARS)
    assert signal.action == "EXIT"
    assert "fake EXIT" in signal.reason


def test_primary_buy_takes_precedence_over_extra():
    composite = CompositeStrategy(FakeStrategy("BUY"), [FakeStrategy("BUY")])
    assert composite.generate_signal("TEST", BARS).reason == "fake BUY"


def test_extra_entry_used_when_primary_holds():
    composite = CompositeStrategy(FakeStrategy("HOLD"), [FakeStrategy("BUY")])
    signal = composite.generate_signal("TEST", BARS)
    assert signal.action == "BUY"


def test_holds_when_nothing_fires():
    composite = CompositeStrategy(FakeStrategy("HOLD"), [FakeStrategy("HOLD")])
    assert composite.generate_signal("TEST", BARS).action == "HOLD"


def test_min_history_is_max_of_all_strategies():
    composite = CompositeStrategy(FakeStrategy("HOLD", 50), [FakeStrategy("HOLD", 201)])
    assert composite.min_history == 201
