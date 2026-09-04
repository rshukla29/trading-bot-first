from __future__ import annotations

import pandas as pd


def rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI. First `period` values are NaN (insufficient history)."""
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


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
