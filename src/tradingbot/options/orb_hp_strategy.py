"""ORB-0DTE-HP: trend-alignment entries on completed 5-minute SPY candles,
trading SPY same-day (0DTE) options. Pure functions shared by the live
bot (run_orb_hp_bot.py) and the backtest (scripts/orb_hp_backtest.py).

CALL when, on one completed 5-min candle:
  - the close is above the 9 EMA, the 50 SMA, the 200 SMA and the session VWAP;
  - the MACD line (12, 26) crosses above its signal line (the 9-period EMA
    of the MACD) on this candle and stays above it;
  - the MACD line is above 0.
PUT: the mirror image (close below all four, MACD crosses below its
signal line and is below 0).
Exit: a call on the first candle that closes red (close < open), a put on
the first that closes green (close > open); flat by 3:55pm.

The 200 SMA spans about 2.5 sessions of 5-min candles, so the indicators run
over several days of history (the VWAP still resets every session).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time

import pandas as pd

from tradingbot.indicators import session_vwap

CALL, PUT = "C", "P"


@dataclass(frozen=True)
class HpParams:
    ema_fast: int = 9
    sma_mid: int = 50
    sma_slow: int = 200
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    @property
    def min_bars(self) -> int:
        return max(self.sma_slow, self.macd_slow + self.macd_signal) + 1


def indicators(bars: pd.DataFrame, p: HpParams) -> pd.DataFrame:
    """open/close plus ema9, sma50, sma200, vwap, macd, signal for every bar.
    `bars` needs open/high/low/close/volume, regular-session only, ET index."""
    c = bars["close"].astype(float)
    ema = lambda s, n: s.ewm(span=n, adjust=False).mean()  # noqa: E731  (TradingView's ta.ema)
    macd = ema(c, p.macd_fast) - ema(c, p.macd_slow)
    return pd.DataFrame({
        "open": bars["open"].astype(float), "close": c,
        "ema9": ema(c, p.ema_fast), "sma50": c.rolling(p.sma_mid).mean(), "sma200": c.rolling(p.sma_slow).mean(),
        "vwap": session_vwap(bars), "macd": macd, "signal": ema(macd, p.macd_signal),
    }, index=bars.index)


def signal_at(ind: pd.DataFrame, i: int, p: HpParams | None = None) -> str | None:
    """CALL / PUT / None on bar `i` (needs bar i-1 for the MACD cross)."""
    if i < 1:
        return None
    now, prev = ind.iloc[i], ind.iloc[i - 1]
    cols = ["ema9", "sma50", "sma200", "vwap", "macd", "signal"]
    if now[cols].isna().any() or prev[["macd", "signal"]].isna().any():
        return None
    c = now.close
    crossed_up = now.macd > now.signal and prev.macd <= prev.signal
    crossed_down = now.macd < now.signal and prev.macd >= prev.signal
    if crossed_up and now.macd > 0 and c > now.ema9 and c > now.sma50 and c > now.sma200 and c > now.vwap:
        return CALL
    if crossed_down and now.macd < 0 and c < now.ema9 and c < now.sma50 and c < now.sma200 and c < now.vwap:
        return PUT
    return None


def latest_signal(bars: pd.DataFrame, p: HpParams) -> tuple[str | None, pd.Series | None]:
    """(signal on the last completed bar, that bar's indicator row)."""
    if len(bars) < p.min_bars:
        return None, None
    ind = indicators(bars, p)
    return signal_at(ind, len(ind) - 1, p), ind.iloc[-1]


def candle_exit(right: str, open_: float, close: float) -> bool:
    """A call exits on a red candle, a put on a green one."""
    return close < open_ if right == CALL else close > open_


def exit_reason(now: time, premium: float, entry: float, *, eod: time, stop_fraction: float | None,
                ladder_stop: float | None, target_fraction: float | None, candle: bool) -> str | None:
    """Why to sell now, or None. Priority: the 3:55pm close, the Rules tab's
    stop loss, step stop and profit target (all off by default), then the
    opposite-colour candle."""
    if now >= eod:
        return "eod_flatten"
    if stop_fraction is not None and premium <= entry * (1 - stop_fraction):
        return "stop_loss"
    if ladder_stop is not None and premium <= ladder_stop:
        return "ladder_stop"
    if target_fraction is not None and premium >= entry * (1 + target_fraction):
        return "profit_target"
    if candle:
        return "candle_exit"
    return None
