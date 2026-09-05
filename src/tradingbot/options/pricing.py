from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(spot: float, strike: float, t_years: float, rate: float, vol: float, right: str) -> float:
    """Black-Scholes price for a European call/put. Used here to simulate
    long-option P&L over a real historical underlying path — European
    pricing ignores early exercise, which is fine because a long holder who
    simply sells to close (never exercises or gets assigned) never hits
    that edge case.
    """
    if t_years <= 0:
        return max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot)
    vol = max(vol, 1e-6)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t_years) / (vol * math.sqrt(t_years))
    d2 = d1 - vol * math.sqrt(t_years)
    if right == "C":
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * t_years) * _norm_cdf(d2)
    return strike * math.exp(-rate * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def bs_delta(spot: float, strike: float, t_years: float, rate: float, vol: float, right: str) -> float:
    if t_years <= 0:
        if right == "C":
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    vol = max(vol, 1e-6)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t_years) / (vol * math.sqrt(t_years))
    return _norm_cdf(d1) if right == "C" else _norm_cdf(d1) - 1


def realized_volatility(closes: pd.Series, window: int, bars_per_year: float) -> pd.Series:
    """Annualized realized volatility from a bar series' log returns,
    used as the implied-vol proxy for bs_price/bs_delta since this backtest
    has no real options-market IV to read. Real IV usually trades at a
    premium to subsequently realized vol (the volatility risk premium) —
    see iv_premium_multiplier in the ORB strategy, which compensates for
    that bias rather than pricing options too cheaply.
    """
    log_returns = np.log(closes / closes.shift(1))
    return log_returns.rolling(window).std() * math.sqrt(bars_per_year)
