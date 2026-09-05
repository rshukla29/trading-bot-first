from __future__ import annotations

import pandas as pd


def rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI. First `period` values are NaN (insufficient history).

    A window with zero losses (avg_loss=0, avg_gain>0) is a legitimate,
    fairly common case for short periods like RSI(2) — it must resolve to
    RSI=100, not NaN. Division naturally gives +inf here (avg_gain/0), which
    correctly resolves the formula to 100 without special-casing. Only the
    true 0/0 case (no gains AND no losses — a perfectly flat window) needs
    an explicit fallback, mapped to the neutral value 50.
    """
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    result = 100 - (100 / (1 + rs))
    no_movement = (avg_gain == 0) & (avg_loss == 0)
    return result.where(~no_movement, 50.0)


def rolling_vwap(bars: pd.DataFrame, window: int = 20) -> pd.Series:
    """Rolling volume-weighted average price over `window` daily bars.

    This is an approximation, not true intraday VWAP (which needs tick or
    intrabar volume data daily bars don't carry) — it's the standard
    swing-trading substitute: a volume-weighted average of typical price
    ((H+L+C)/3) over a rolling window, read the same way daily-chart VWAP
    overlays are: price above it is the volume-weighted bullish side,
    below is bearish, and a pullback to it that holds is a "VWAP retest."
    """
    typical_price = (bars["high"] + bars["low"] + bars["close"]) / 3
    pv = typical_price * bars["volume"]
    return pv.rolling(window).sum() / bars["volume"].rolling(window).sum()


def avg_volume(volume: pd.Series, window: int = 20) -> pd.Series:
    return volume.rolling(window).mean()


def fib_retracement_fraction(bars: pd.DataFrame, lookback: int) -> pd.Series:
    """How far each bar's close has retraced from the `lookback`-window
    swing high back toward the swing low, as a fraction: 0.0 = sitting at
    the high, 1.0 = all the way back down at the low. The classic
    "golden zone" (0.382-0.618) is the standard confluence check for a
    pullback landing at a plausible support level rather than an arbitrary
    price. Values outside [0, 1] mean price broke past the high or low
    within the lookback window itself.
    """
    swing_high = bars["high"].rolling(lookback).max()
    swing_low = bars["low"].rolling(lookback).min()
    span = (swing_high - swing_low).replace(0, float("nan"))
    return (swing_high - bars["close"]) / span
