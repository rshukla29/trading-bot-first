import pandas as pd

from tradingbot.indicators import avg_volume, rolling_vwap, rsi


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


def pytest_approx(value: float, tol: float = 1e-9):
    import pytest

    return pytest.approx(value, abs=tol)
