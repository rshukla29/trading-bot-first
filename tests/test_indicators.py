import pytest
import pandas as pd

from tradingbot.indicators import avg_volume, fib_retracement_fraction, rolling_vwap, rsi, volume_spike_ratio


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


def test_rsi_is_high_for_mostly_rising_series():
    closes = pd.Series([100, 102, 101, 104, 106, 108, 110, 112, 114, 116.0])
    values = rsi(closes, period=3)
    assert values.iloc[-1] > 80


def test_rsi_is_low_for_mostly_falling_series():
    closes = pd.Series([116, 114, 115, 112, 110, 108, 106, 104, 102, 100.0])
    values = rsi(closes, period=3)
    assert values.iloc[-1] < 20


def test_rsi_warmup_is_nan():
    closes = pd.Series([100, 101, 102, 103, 104.0])
    values = rsi(closes, period=3)
    assert values.iloc[:2].isna().all()


def test_rolling_vwap_equals_mean_close_when_volume_constant():
    # make_bars sets high=1.01c and low=0.99c, so typical price ((H+L+C)/3)
    # is exactly the close; with constant volume the VWAP reduces to a plain
    # rolling mean of closes.
    closes = [100.0, 110, 120, 130, 140]
    bars = make_bars(closes)
    result = rolling_vwap(bars, window=3).iloc[-1]
    assert result == pytest_approx(sum(closes[-3:]) / 3)


def test_rolling_vwap_is_pulled_toward_high_volume_bars():
    closes = [100.0, 100, 200]
    bars = make_bars(closes, volumes=[1.0, 1.0, 8.0])
    result = rolling_vwap(bars, window=3).iloc[-1]
    # (100*1 + 100*1 + 200*8) / 10 = 180
    assert result == pytest_approx(180.0)


def test_avg_volume_rolling_mean():
    volume = pd.Series([100.0, 200, 300, 400])
    assert avg_volume(volume, window=2).iloc[-1] == 350.0


def test_rsi_is_100_when_window_has_no_losses():
    # A window with only gains (or flat bars) must resolve to RSI=100, not
    # NaN — regression guard for a bug where an explicit avg_loss.replace(0,
    # NaN) forced this legitimate case to NaN instead of the mathematically
    # correct value.
    closes = pd.Series([100.0] * 9 + [101.0, 108.0])
    values = rsi(closes, period=2)
    assert values.iloc[-1] == 100.0


def test_rsi_is_0_when_window_has_no_gains():
    closes = pd.Series([116.0] * 9 + [114.0, 108.0])
    values = rsi(closes, period=2)
    assert values.iloc[-1] == 0.0


def test_rsi_is_neutral_when_perfectly_flat():
    closes = pd.Series([100.0] * 10)
    values = rsi(closes, period=2)
    assert values.iloc[-1] == 50.0


def _fixed_range_bars(closes: list[float], fixed_high: float, fixed_low: float) -> pd.DataFrame:
    """Bars with an explicit, constant high/low ceiling and floor (rather
    than make_bars' close-relative +-1% offset) so the swing high/low used
    by fib_retracement_fraction land exactly where a test expects."""
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="B")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [fixed_high] * len(closes),
            "low": [fixed_low] * len(closes),
            "close": closes,
            "volume": [1_000_000.0] * len(closes),
        },
        index=dates,
    )


def test_fib_retracement_at_the_swing_low_is_fraction_one():
    bars = _fixed_range_bars([100.0, 95, 90, 85, 80], fixed_high=100.0, fixed_low=80.0)
    frac = fib_retracement_fraction(bars, lookback=5).iloc[-1]
    assert frac == pytest_approx(1.0)


def test_fib_retracement_at_the_swing_high_is_fraction_zero():
    bars = _fixed_range_bars([80.0, 85, 90, 95, 100], fixed_high=100.0, fixed_low=80.0)
    frac = fib_retracement_fraction(bars, lookback=5).iloc[-1]
    assert frac == pytest_approx(0.0)


def test_fib_retracement_halfway_is_fraction_half():
    # high column is fixed at 100 (via a flat top bar) and low fixed at 80,
    # with the final close sitting exactly at the midpoint.
    dates = pd.date_range("2024-01-01", periods=5, freq="B")
    bars = pd.DataFrame(
        {
            "open": [100.0, 100, 100, 100, 90.0],
            "high": [100.0, 100, 100, 100, 100.0],
            "low": [80.0, 80, 80, 80, 80.0],
            "close": [100.0, 95, 90, 85, 90.0],
            "volume": [1_000_000.0] * 5,
        },
        index=dates,
    )
    frac = fib_retracement_fraction(bars, lookback=5).iloc[-1]
    assert frac == pytest_approx(0.5)


def pytest_approx(value: float, tol: float = 1e-9):
    import pytest

    return pytest.approx(value, abs=tol)


def test_volume_spike_ratio_flags_extreme_day():
    # 20 normal days at 1M volume, then a 21.5x spike (matches the real
    # PMTS incident that motivated this filter).
    volumes = [1_000_000.0] * 20 + [21_500_000.0]
    bars = make_bars([100.0] * 21, volumes=volumes)
    ratio = volume_spike_ratio(bars["volume"], window=20).iloc[-1]
    assert ratio == pytest_approx(21.5)


def test_volume_spike_ratio_normal_day_is_low():
    volumes = [1_000_000.0] * 20 + [1_750_000.0]  # matches the real OMDA day: 1.75x, not a spike
    bars = make_bars([100.0] * 21, volumes=volumes)
    ratio = volume_spike_ratio(bars["volume"], window=20).iloc[-1]
    assert ratio == pytest_approx(1.75)


def test_volume_spike_ratio_excludes_today_from_its_own_baseline():
    # If today's huge volume leaked into its own baseline, the ratio would
    # be understated. 20 days at 1M, then today at 21M: baseline must stay
    # 1M (today excluded), giving ratio 21x, not something smaller.
    volumes = [1_000_000.0] * 20 + [21_000_000.0]
    bars = make_bars([100.0] * 21, volumes=volumes)
    ratio = volume_spike_ratio(bars["volume"], window=20).iloc[-1]
    assert ratio == pytest_approx(21.0)


def test_session_vwap_resets_each_day():
    from tradingbot.indicators import session_vwap

    idx = pd.to_datetime(["2024-01-02 09:30", "2024-01-02 09:35", "2024-01-03 09:30"])
    bars = pd.DataFrame(
        {"high": [10.0, 20.0, 50.0], "low": [10.0, 20.0, 50.0], "close": [10.0, 20.0, 50.0],
         "volume": [100.0, 300.0, 10.0]},
        index=idx,
    )
    vwap = session_vwap(bars)
    assert vwap.iloc[0] == 10.0
    assert vwap.iloc[1] == pytest.approx((10 * 100 + 20 * 300) / 400)  # 17.5
    assert vwap.iloc[2] == 50.0  # new day: previous day's volume doesn't carry over
