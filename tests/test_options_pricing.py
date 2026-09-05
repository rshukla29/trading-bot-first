import math

import pandas as pd
import pytest

from tradingbot.options.pricing import bs_delta, bs_price, realized_volatility


def test_call_price_increases_with_spot():
    low = bs_price(spot=90, strike=100, t_years=0.5, rate=0.04, vol=0.2, right="C")
    high = bs_price(spot=110, strike=100, t_years=0.5, rate=0.04, vol=0.2, right="C")
    assert high > low


def test_put_price_decreases_with_spot():
    low = bs_price(spot=90, strike=100, t_years=0.5, rate=0.04, vol=0.2, right="P")
    high = bs_price(spot=110, strike=100, t_years=0.5, rate=0.04, vol=0.2, right="P")
    assert high < low


def test_put_call_parity():
    spot, strike, t_years, rate, vol = 100.0, 100.0, 0.5, 0.05, 0.2
    call = bs_price(spot, strike, t_years, rate, vol, "C")
    put = bs_price(spot, strike, t_years, rate, vol, "P")
    parity_rhs = spot - strike * math.exp(-rate * t_years)
    assert call - put == pytest.approx(parity_rhs, abs=1e-6)


def test_price_at_expiry_is_intrinsic_value():
    assert bs_price(100, 90, 0, 0.05, 0.2, "C") == pytest.approx(10.0)
    assert bs_price(100, 90, 0, 0.05, 0.2, "P") == pytest.approx(0.0)
    assert bs_price(90, 100, 0, 0.05, 0.2, "P") == pytest.approx(10.0)


def test_call_delta_bounds():
    deep_itm = bs_delta(200, 100, 0.1, 0.04, 0.2, "C")
    deep_otm = bs_delta(50, 100, 0.1, 0.04, 0.2, "C")
    assert deep_itm > 0.9
    assert deep_otm < 0.1


def test_put_delta_bounds():
    deep_itm = bs_delta(50, 100, 0.1, 0.04, 0.2, "P")
    deep_otm = bs_delta(200, 100, 0.1, 0.04, 0.2, "P")
    assert deep_itm < -0.9
    assert deep_otm > -0.1


def test_realized_volatility_warmup_is_nan():
    closes = pd.Series([100.0, 101, 102, 103, 104])
    vol = realized_volatility(closes, window=3, bars_per_year=252)
    assert vol.iloc[:2].isna().all()


def test_realized_volatility_zero_for_constant_growth():
    # A perfectly constant per-step return has zero variance -> zero vol.
    closes = pd.Series([100.0 * (1.001**i) for i in range(30)])
    vol = realized_volatility(closes, window=10, bars_per_year=252)
    assert vol.iloc[-1] == pytest.approx(0.0, abs=1e-9)


def test_realized_volatility_positive_for_noisy_series():
    closes = pd.Series([100.0, 105, 98, 107, 96, 110, 94, 112, 93, 115])
    vol = realized_volatility(closes, window=5, bars_per_year=252)
    assert vol.iloc[-1] > 0
