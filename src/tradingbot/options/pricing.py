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


def find_strike_for_delta(
    spot: float,
    t_years: float,
    rate: float,
    vol: float,
    right: str,
    target_delta_abs: float,
    strike_increment: float = 1.0,
    max_iter: int = 100,
) -> float:
    """Strike whose Black-Scholes delta magnitude is closest to
    target_delta_abs (e.g. 0.16 for a "16-delta" short strike). bs_delta is
    monotonically decreasing in strike for both calls (1 -> 0) and puts
    (0 -> -1), so comparing against a signed target lets one bisection
    search handle both.
    """
    target_signed = target_delta_abs if right == "C" else -target_delta_abs
    lo, hi = spot * 0.3, spot * 3.0
    mid = spot
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        delta = bs_delta(spot, mid, t_years, rate, vol, right)
        if abs(delta - target_signed) < 1e-4:
            break
        if delta > target_signed:
            lo = mid
        else:
            hi = mid
    return round(mid / strike_increment) * strike_increment


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


def vol_percentile(vol_series: pd.Series, lookback: int) -> pd.Series:
    """Each value's percentile rank (0-1) within its own trailing `lookback`
    window — a realized-vol-based stand-in for IV rank, used to time premium
    sales for when vol is relatively rich versus its own recent history
    (vol is well known to be mean-reverting, so this is the standard
    volatility-selling timing heuristic even without real IV data)."""
    return vol_series.rolling(lookback).apply(lambda w: (w[-1] > w).mean(), raw=True)


# 0DTE SPY implied vol ~ VIX x 1.07 with 60+ minutes left, ramping linearly
# to ~2.9x that at the 4:00pm close. Calibrated on 14 real ORB fills
# (analysis/2026-10-01_orb_loss_analysis). The realized-vol model above
# underpriced those fills by a median 1.4x and 3-8x near the close.
VIX_IV_MULTIPLIER = 1.07
VIX_CLOSE_RAMP = 1.89


def implied_vol(price: float, spot: float, strike: float, t_years: float, rate: float, right: str) -> float:
    """Black-Scholes implied vol of an option price (bisection, 1%..500%)."""
    lo, hi = 0.01, 5.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, t_years, rate, mid, right) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def vix_calibrated_vol(vix_level: float, minutes_left: float,
                       multiplier: float = VIX_IV_MULTIPLIER, close_ramp: float = VIX_CLOSE_RAMP) -> float:
    """Estimated 0DTE implied vol from the VIX level (e.g. 17.5) and the
    minutes left to the close."""
    ramp = 1 + close_ramp * max(0.0, (60 - minutes_left) / 60)
    return vix_level / 100 * multiplier * ramp


def vix_vol_series(bar_index: pd.DatetimeIndex, vix_close: pd.Series, bar_minutes: int = 5) -> pd.Series:
    """vix_calibrated_vol for every bar (known at the bar's close) — the
    backtest's realistic pricing input (run_options_backtest vol_override)."""
    vix = vix_close.reindex(bar_index).ffill()
    bar_close = bar_index + pd.Timedelta(minutes=bar_minutes)
    mins = ((bar_close.normalize() + pd.Timedelta(hours=16)) - bar_close).total_seconds() / 60
    mins = pd.Series(mins, index=bar_index).clip(lower=1)
    ramp = 1 + VIX_CLOSE_RAMP * ((60 - mins).clip(lower=0) / 60)
    return vix / 100 * VIX_IV_MULTIPLIER * ramp
