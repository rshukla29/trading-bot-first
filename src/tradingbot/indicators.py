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


def volume_spike_ratio(volume: pd.Series, window: int = 20) -> pd.Series:
    """Each bar's volume as a multiple of the average of the *preceding*
    `window` bars — today's own volume is excluded from its baseline
    (same lagging convention used for the realized-volatility estimate in
    options/pricing.py), otherwise an extreme volume day would inflate
    its own baseline and understate its own ratio.

    A high ratio is the signature of a news/event-driven move (earnings,
    FDA action, etc.) rather than routine technical price action — used
    to filter RSI(2) mean-reversion entries, since the "buy the oversold
    dip" edge is a bet on short-term technical overextension snapping
    back, an assumption that doesn't reliably transfer to a stock that
    just re-rated on real new information.
    """
    baseline = volume.shift(1).rolling(window).mean()
    return volume / baseline


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


def session_vwap(bars: pd.DataFrame) -> pd.Series:
    """Intraday VWAP anchored to each session's first bar — the standard
    day-trading VWAP (unlike rolling_vwap above, which is a multi-day
    daily-bar approximation). Resets every calendar day, so bars must be
    regular-session only and indexed in exchange-local time. Each bar's
    value includes that bar, i.e. it's known at the bar's close."""
    typical_price = (bars["high"] + bars["low"] + bars["close"]) / 3
    day = pd.Index(bars.index.date)
    pv = (typical_price * bars["volume"]).groupby(day).cumsum()
    vol = bars["volume"].groupby(day).cumsum()
    return pd.Series(pv.to_numpy() / vol.to_numpy(), index=bars.index)


FIB_RETRACEMENTS = (0.236, 0.382, 0.5, 0.618, 0.786)
FIB_EXTENSIONS = (1.272, 1.618)


def fib_levels(swing_high: float, swing_low: float) -> dict[str, float]:
    """Fibonacci levels for a swing — the same math as the Fibonacci tool on
    IBKR/TradingView charts (the API can't read chart drawings, so the
    levels are computed from price data). Retracements are measured down
    from the high ("38.2%" = high - 0.382 x range); extensions project the
    range beyond each end ("127.2% up" = low + 1.272 x range)."""
    span = swing_high - swing_low
    levels = {f"{r * 100:g}%": swing_high - r * span for r in FIB_RETRACEMENTS}
    for e in FIB_EXTENSIONS:
        levels[f"{e * 100:g}% up"] = swing_low + e * span
        levels[f"{e * 100:g}% down"] = swing_high - e * span
    return levels


def fib_zone(price: float, swing_high: float, swing_low: float) -> str:
    """Where `price` sits within the swing, in retracement terms."""
    span = swing_high - swing_low
    if span <= 0:
        return "flat"
    if price > swing_high:
        return f"above high (ext {((price - swing_low) / span) * 100:.0f}%)"
    if price < swing_low:
        return f"below low (ext {((swing_high - price) / span) * 100:.0f}%)"
    frac = (swing_high - price) / span
    if frac < 0.382:
        return f"shallow pullback ({frac * 100:.0f}%)"
    if frac <= 0.618:
        return f"golden zone 38.2-61.8% ({frac * 100:.0f}%)"
    return f"deep retracement ({frac * 100:.0f}%)"
