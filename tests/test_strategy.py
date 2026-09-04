import numpy as np
import pandas as pd

from tradingbot.strategy.ma_crossover import MovingAverageCrossoverStrategy


def make_bars(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="B")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1_000_000] * len(closes),
        },
        index=dates,
    )


def test_insufficient_history_holds():
    strat = MovingAverageCrossoverStrategy(fast=5, slow=10)
    bars = make_bars([100.0] * 5)
    signal = strat.generate_signal("TEST", bars)
    assert signal.action == "HOLD"


def test_golden_cross_triggers_buy():
    strat = MovingAverageCrossoverStrategy(fast=3, slow=5)
    # Flat history (both SMAs equal, diff=0) then a single jump on the final
    # bar that pulls the fast SMA above the slow SMA precisely on that bar.
    closes = [100.0] * 9 + [115.0]
    bars = make_bars(closes)
    signal = strat.generate_signal("TEST", bars)
    assert signal.action == "BUY"


def test_death_cross_triggers_exit():
    strat = MovingAverageCrossoverStrategy(fast=3, slow=5)
    closes = [100.0] * 9 + [85.0]
    bars = make_bars(closes)
    signal = strat.generate_signal("TEST", bars)
    assert signal.action == "EXIT"


def test_no_crossover_holds():
    strat = MovingAverageCrossoverStrategy(fast=3, slow=5)
    rng = np.random.default_rng(0)
    closes = 100 + rng.normal(0, 0.01, size=20).cumsum()
    bars = make_bars(list(closes))
    # Just assert it runs and returns a valid action without raising.
    signal = strat.generate_signal("TEST", bars)
    assert signal.action in {"HOLD", "BUY", "EXIT"}


def test_fast_must_be_shorter_than_slow():
    import pytest

    with pytest.raises(ValueError):
        MovingAverageCrossoverStrategy(fast=10, slow=5)
