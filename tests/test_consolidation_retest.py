import pandas as pd

from tradingbot.strategy.consolidation_retest import ConsolidationRetestStrategy


def make_bars(closes: list[float], volumes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="B")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": volumes,
        },
        index=dates,
    )


def strategy() -> ConsolidationRetestStrategy:
    return ConsolidationRetestStrategy(
        consolidation_window=5,
        max_range_pct=0.06,
        max_bars_since_breakout=3,
        retest_tolerance=0.02,
        volume_multiplier=1.2,
    )


# 3 padding bars, then a 5-bar consolidation (range high ~102.01), then the
# breakout bar, then today's bar. Volume on the breakout is 2x the
# consolidation average.
PADDING = [95.0, 96, 97]
CONSOLIDATION = [100.0, 101, 100.5, 99.5, 100.2]
BASE_VOLUMES = [1_000_000.0] * 8


def test_retest_that_holds_triggers_buy():
    closes = PADDING + CONSOLIDATION + [106.0, 103.0]
    volumes = BASE_VOLUMES + [2_000_000.0, 1_000_000.0]
    signal = strategy().generate_signal("TEST", make_bars(closes, volumes))
    assert signal.action == "BUY"
    assert "retested and held" in signal.reason


def test_no_pullback_means_no_retest_entry():
    # Price ran away from the breakout level instead of coming back to it.
    closes = PADDING + CONSOLIDATION + [106.0, 115.0]
    volumes = BASE_VOLUMES + [2_000_000.0, 1_000_000.0]
    assert strategy().generate_signal("TEST", make_bars(closes, volumes)).action == "HOLD"


def test_failed_breakout_voids_the_setup():
    # Closed back below the breakout level before the retest — a failed
    # breakout, not a retest, so the later bounce must not be bought.
    closes = PADDING + CONSOLIDATION + [106.0, 100.0, 103.0]
    volumes = BASE_VOLUMES + [2_000_000.0, 1_000_000.0, 1_000_000.0]
    assert strategy().generate_signal("TEST", make_bars(closes, volumes)).action == "HOLD"


def test_low_volume_breakout_is_rejected():
    closes = PADDING + CONSOLIDATION + [106.0, 103.0]
    volumes = BASE_VOLUMES + [1_000_000.0, 1_000_000.0]  # breakout at 1.0x, needs 1.2x
    assert strategy().generate_signal("TEST", make_bars(closes, volumes)).action == "HOLD"


def test_wide_range_is_not_a_consolidation():
    wide = [90.0, 100, 95, 105, 98]
    closes = PADDING + wide + [112.0, 107.0]
    volumes = BASE_VOLUMES + [2_000_000.0, 1_000_000.0]
    assert strategy().generate_signal("TEST", make_bars(closes, volumes)).action == "HOLD"


def test_insufficient_history_holds():
    closes = [100.0] * 5
    assert strategy().generate_signal("TEST", make_bars(closes, [1e6] * 5)).action == "HOLD"


def test_min_history():
    assert strategy().min_history == 10
