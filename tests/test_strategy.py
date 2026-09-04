import numpy as np
import pandas as pd
import pytest

from tradingbot.strategy.ma_crossover import MovingAverageCrossoverStrategy


def make_bars(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    volumes = volumes or [1_000_000.0] * len(closes)
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


def bare(fast: int = 3, slow: int = 5, **overrides) -> MovingAverageCrossoverStrategy:
    """Crossover with every confirmation filter off unless a test enables one,
    so each test exercises exactly one piece of logic."""
    kwargs = dict(trend_ma=None, volume_ma=None, rsi_period=None, vwap_window=None)
    kwargs.update(overrides)
    return MovingAverageCrossoverStrategy(fast=fast, slow=slow, **kwargs)


# A decline into a bounce that produces a golden cross on the final bar.
# Reused across filter tests: the crossover always fires, so whatever the
# test asserts is attributable to the filter under test.
DECLINE_THEN_BOUNCE = [300.0, 250, 200, 180, 170, 165, 160, 158, 157, 156]


def test_insufficient_history_holds():
    strat = bare(fast=5, slow=10)
    bars = make_bars([100.0] * 5)
    assert strat.generate_signal("TEST", bars).action == "HOLD"


def test_golden_cross_triggers_buy():
    strat = bare()
    closes = [100.0] * 9 + [115.0]
    assert strat.generate_signal("TEST", make_bars(closes)).action == "BUY"


def test_death_cross_triggers_exit():
    strat = bare()
    closes = [100.0] * 9 + [85.0]
    assert strat.generate_signal("TEST", make_bars(closes)).action == "EXIT"


def test_no_crossover_holds():
    strat = bare()
    rng = np.random.default_rng(0)
    closes = 100 + rng.normal(0, 0.01, size=20).cumsum()
    assert strat.generate_signal("TEST", make_bars(list(closes))).action in {"HOLD", "BUY", "EXIT"}


def test_fast_must_be_shorter_than_slow():
    with pytest.raises(ValueError):
        MovingAverageCrossoverStrategy(fast=10, slow=5)


# --- trend filter ---

def test_trend_filter_blocks_golden_cross_below_trend():
    # Bounce to 165 is a golden cross, but 165 sits below the 10-period
    # trend SMA (176.1) — a dead-cat bounce inside a downtrend.
    strat = bare(trend_ma=10)
    signal = strat.generate_signal("TEST", make_bars(DECLINE_THEN_BOUNCE + [165.0]))
    assert signal.action == "HOLD"
    assert "trend filter" in signal.reason


def test_trend_filter_allows_golden_cross_above_trend():
    # Same setup bouncing to 200, above the 10-period trend SMA (179.6).
    strat = bare(trend_ma=10)
    assert strat.generate_signal("TEST", make_bars(DECLINE_THEN_BOUNCE + [200.0])).action == "BUY"


def test_trend_filter_does_not_block_exits():
    strat = bare(trend_ma=10)
    closes = [90.0, 92, 94, 96, 98, 100, 102, 104, 106, 108] + [70.0]
    assert strat.generate_signal("TEST", make_bars(closes)).action == "EXIT"


# --- volume filter ---

def test_volume_filter_blocks_when_participation_is_fading():
    # Recent 3-day average volume (0.4M) is below the 6-day baseline, i.e.
    # the move is happening on drying-up participation.
    strat = bare(volume_ma=6, volume_fast_ma=3)
    closes = [100.0] * 9 + [115.0]
    volumes = [1_000_000.0] * 7 + [400_000.0] * 3
    signal = strat.generate_signal("TEST", make_bars(closes, volumes))
    assert signal.action == "HOLD"
    assert "volume" in signal.reason


def test_volume_filter_allows_when_participation_is_rising():
    strat = bare(volume_ma=6, volume_fast_ma=3)
    closes = [100.0] * 9 + [115.0]
    volumes = [1_000_000.0] * 7 + [2_000_000.0] * 3
    signal = strat.generate_signal("TEST", make_bars(closes, volumes))
    assert signal.action == "BUY"
    assert "volume" in signal.reason


def test_volume_filter_does_not_require_a_spike_on_the_crossover_bar():
    # Steady, flat volume throughout: participation is holding (1.0x
    # baseline), so a lagging crossover signal must still be allowed —
    # this is the regression guard for the over-filtering bug where
    # demanding a single-bar volume spike rejected most valid signals.
    strat = bare(volume_ma=6, volume_fast_ma=3)
    closes = [100.0] * 9 + [115.0]
    assert strat.generate_signal("TEST", make_bars(closes)).action == "BUY"


# --- RSI filter ---

def test_rsi_filter_blocks_overbought_entry():
    strat = bare(rsi_period=3, rsi_overbought=70)
    signal = strat.generate_signal("TEST", make_bars(DECLINE_THEN_BOUNCE + [200.0]))
    assert signal.action == "HOLD"
    assert "overbought" in signal.reason


def test_rsi_filter_allows_entry_below_threshold():
    # Same bars, threshold raised above the bar's RSI — isolates the
    # threshold as the only thing that changed.
    strat = bare(rsi_period=3, rsi_overbought=99.9)
    assert strat.generate_signal("TEST", make_bars(DECLINE_THEN_BOUNCE + [200.0])).action == "BUY"


def test_rsi_exhaustion_triggers_exit():
    # Steady uptrend (RSI > 80) then a pullback that knocks RSI back below
    # 80, while the fast SMA is still above the slow SMA (so this is the
    # RSI exit firing, not a death cross).
    strat = bare(rsi_period=3, rsi_exit_overbought=80)
    closes = [100.0, 102, 101, 104, 106, 108, 110, 112, 114, 116, 118, 120, 122, 124, 118]
    signal = strat.generate_signal("TEST", make_bars(closes))
    assert signal.action == "EXIT"
    assert "RSI exhaustion" in signal.reason


# --- VWAP filter ---

def test_vwap_filter_blocks_entry_below_vwap():
    # Constant volume + make_bars geometry means VWAP == rolling mean close
    # (176.1 here), so the 165 bounce is below VWAP.
    strat = bare(vwap_window=10)
    signal = strat.generate_signal("TEST", make_bars(DECLINE_THEN_BOUNCE + [165.0]))
    assert signal.action == "HOLD"
    assert "VWAP" in signal.reason


def test_vwap_filter_allows_entry_above_vwap():
    strat = bare(vwap_window=10)
    signal = strat.generate_signal("TEST", make_bars(DECLINE_THEN_BOUNCE + [200.0]))
    assert signal.action == "BUY"
    assert "above VWAP" in signal.reason


def test_vwap_retest_is_flagged_in_reason():
    # Close just above VWAP so the bar's low dips within the retest
    # tolerance of it — the "pullback tagged VWAP and held" case.
    strat = bare(vwap_window=10, vwap_retest_tolerance=0.05)
    signal = strat.generate_signal("TEST", make_bars(DECLINE_THEN_BOUNCE + [178.0]))
    assert signal.action == "BUY"
    assert "VWAP retest held" in signal.reason


def test_min_history_accounts_for_every_enabled_filter():
    strat = MovingAverageCrossoverStrategy(
        fast=20, slow=50, trend_ma=200, volume_ma=20, rsi_period=14, vwap_window=20
    )
    assert strat.min_history == 201
    assert bare(fast=3, slow=5).min_history == 6
