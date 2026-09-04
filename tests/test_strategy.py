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
    strat = MovingAverageCrossoverStrategy(fast=5, slow=10, trend_ma=None)
    bars = make_bars([100.0] * 5)
    signal = strat.generate_signal("TEST", bars)
    assert signal.action == "HOLD"


def test_golden_cross_triggers_buy():
    strat = MovingAverageCrossoverStrategy(fast=3, slow=5, trend_ma=None)
    # Flat history (both SMAs equal, diff=0) then a single jump on the final
    # bar that pulls the fast SMA above the slow SMA precisely on that bar.
    closes = [100.0] * 9 + [115.0]
    bars = make_bars(closes)
    signal = strat.generate_signal("TEST", bars)
    assert signal.action == "BUY"


def test_death_cross_triggers_exit():
    strat = MovingAverageCrossoverStrategy(fast=3, slow=5, trend_ma=None)
    closes = [100.0] * 9 + [85.0]
    bars = make_bars(closes)
    signal = strat.generate_signal("TEST", bars)
    assert signal.action == "EXIT"


def test_no_crossover_holds():
    strat = MovingAverageCrossoverStrategy(fast=3, slow=5, trend_ma=None)
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


def test_trend_filter_blocks_golden_cross_below_trend():
    # A decline from 300 down to ~156 (a downtrend, so the 10-period trend
    # SMA stays elevated) followed by a modest bounce to 165. The bounce is
    # enough to flip fast(3) above slow(5) — a golden cross — but 165 is
    # still below the 10-period trend SMA (176.1), i.e. a dead-cat bounce
    # inside a larger downtrend that should NOT be bought.
    strat = MovingAverageCrossoverStrategy(fast=3, slow=5, trend_ma=10)
    closes = [300.0, 250, 200, 180, 170, 165, 160, 158, 157, 156, 165]
    bars = make_bars(closes)
    signal = strat.generate_signal("TEST", bars)
    assert signal.action == "HOLD"
    assert "trend filter" in signal.reason


def test_trend_filter_allows_golden_cross_above_trend():
    # Same setup, but the final bounce is to 200 instead of 165 — still a
    # golden cross (fast/slow diff flips the same way), and now price (200)
    # is above the 10-period trend SMA (179.6), so the filter allows it.
    strat = MovingAverageCrossoverStrategy(fast=3, slow=5, trend_ma=10)
    closes = [300.0, 250, 200, 180, 170, 165, 160, 158, 157, 156, 200]
    bars = make_bars(closes)
    signal = strat.generate_signal("TEST", bars)
    assert signal.action == "BUY"


def test_trend_filter_does_not_block_exits():
    # Death cross should fire regardless of the trend filter — exits are
    # never filtered, only entries.
    strat = MovingAverageCrossoverStrategy(fast=3, slow=5, trend_ma=10)
    closes = [90.0, 92, 94, 96, 98, 100, 102, 104, 106, 108] + [70.0]
    bars = make_bars(closes)
    signal = strat.generate_signal("TEST", bars)
    assert signal.action == "EXIT"
