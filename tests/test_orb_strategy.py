import pandas as pd
import pytest

from tradingbot.options.orb_strategy import OpeningRangeBreakoutStrategy


def make_bars(closes: list[float], start: str = "2024-01-02 09:30", freq: str = "5min") -> pd.DataFrame:
    dates = pd.date_range(start, periods=len(closes), freq=freq)
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * len(closes),
        },
        index=dates,
    )


def strategy() -> OpeningRangeBreakoutStrategy:
    return OpeningRangeBreakoutStrategy(opening_range_minutes=15, bar_minutes=5)


# 3 range bars (09:30-09:40) establishing range [99.5, 100.5], then post-range bars.
RANGE = [100.0, 100.0, 100.0]


def test_invalid_minutes_combination_raises():
    with pytest.raises(ValueError):
        OpeningRangeBreakoutStrategy(opening_range_minutes=17, bar_minutes=5)


def test_insufficient_bars_holds():
    bars = make_bars(RANGE[:2])
    signal = strategy().generate_signal("SPY", bars)
    assert signal.action == "HOLD"
    assert "forming" in signal.reason


def test_breakout_up_triggers_buy_call():
    bars = make_bars(RANGE + [99.8, 101.0])
    signal = strategy().generate_signal("SPY", bars)
    assert signal.action == "BUY_CALL"


def test_second_bar_above_range_does_not_refire():
    bars = make_bars(RANGE + [99.8, 101.0, 101.5])
    signal = strategy().generate_signal("SPY", bars)
    assert signal.action == "HOLD"
    assert "already-broken" in signal.reason


def test_breakdown_triggers_buy_put():
    bars = make_bars(RANGE + [99.8, 98.0])
    signal = strategy().generate_signal("SPY", bars)
    assert signal.action == "BUY_PUT"


def test_inside_range_holds():
    bars = make_bars(RANGE + [99.8, 100.2])
    signal = strategy().generate_signal("SPY", bars)
    assert signal.action == "HOLD"


def test_new_day_gets_a_fresh_range():
    # Day 1: breaks up and would not refire. Day 2 starts a brand new range.
    day1 = make_bars(RANGE + [99.8, 101.0], start="2024-01-02 09:30")
    day2 = make_bars([50.0, 50.0, 50.0, 49.8, 52.0], start="2024-01-03 09:30")
    bars = pd.concat([day1, day2])
    signal = strategy().generate_signal("SPY", bars)
    assert signal.action == "BUY_CALL"


def test_range_for_day_returns_none_when_insufficient():
    bars = make_bars(RANGE[:2])
    assert strategy().range_for_day(bars, bars.index[0].date()) is None


def test_range_for_day_returns_correct_range():
    bars = make_bars(RANGE + [99.8, 101.0])
    result = strategy().range_for_day(bars, bars.index[0].date())
    assert result == (99.5, 100.5)


def test_min_history():
    assert strategy().min_history == 4
