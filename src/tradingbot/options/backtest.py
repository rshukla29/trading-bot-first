from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time, timedelta

import pandas as pd

from tradingbot.options.orb_strategy import OpeningRangeBreakoutStrategy
from tradingbot.options.pricing import bs_price, realized_volatility

TRADING_MINUTES_PER_DAY = 390  # 9:30-16:00 ET
TRADING_DAYS_PER_YEAR = 252
CONTRACT_MULTIPLIER = 100


@dataclass
class OptionTrade:
    symbol: str
    right: str  # "C" or "P"
    entry_time: pd.Timestamp
    entry_underlying: float
    strike: float
    expiry: pd.Timestamp
    entry_premium: float
    contracts: int
    exit_time: pd.Timestamp | None = None
    exit_underlying: float | None = None
    exit_premium: float | None = None
    exit_reason: str | None = None

    @property
    def pnl(self) -> float | None:
        if self.exit_premium is None:
            return None
        return (self.exit_premium - self.entry_premium) * self.contracts * CONTRACT_MULTIPLIER


@dataclass
class OptionsBacktestResult:
    trades: list[OptionTrade] = field(default_factory=list)
    equity_curve: list[tuple[pd.Timestamp, float]] = field(default_factory=list)
    starting_equity: float = 0.0
    ending_equity: float = 0.0

    @property
    def closed_trades(self) -> list[OptionTrade]:
        return [t for t in self.trades if t.exit_premium is not None]

    @property
    def win_rate(self) -> float:
        closed = self.closed_trades
        if not closed:
            return 0.0
        return sum(1 for t in closed if (t.pnl or 0) > 0) / len(closed)

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl or 0 for t in self.closed_trades)

    @property
    def total_return_pct(self) -> float:
        if self.starting_equity == 0:
            return 0.0
        return (self.ending_equity - self.starting_equity) / self.starting_equity


def _time_to_expiry_years(now: pd.Timestamp, expiry: pd.Timestamp) -> float:
    return max(0.0, (expiry - now).total_seconds() / (365.25 * 24 * 3600))


def run_options_backtest(
    symbol: str,
    bars: pd.DataFrame,
    strategy: OpeningRangeBreakoutStrategy,
    starting_equity: float,
    risk_pct_per_trade: float,
    stop_loss_pct_of_premium: float,
    profit_target_pct: float | None,
    dte_days: int,
    otm_pct: float,
    strike_increment: float,
    vol_window: int,
    iv_premium_multiplier: float,
    rate: float = 0.04,
    exit_on_range_reentry: bool = True,
    eod_flatten_time: time = time(15, 55),
    warmup: int = 0,
) -> OptionsBacktestResult:
    """Intraday-only long-options backtest: opening-range-breakout entries,
    Black-Scholes-simulated premiums (see options/pricing.py — no real
    historical options data was available on this account), always flat by
    end of day regardless of the chosen expiry's DTE. dte_days only changes
    which contract's theta/extrinsic-value profile is simulated, not how
    long the trade is held.
    """
    bars_per_year = (TRADING_MINUTES_PER_DAY / strategy.bar_minutes) * TRADING_DAYS_PER_YEAR
    # Shifted by one bar: price bar i using volatility measured through bar
    # i-1 only. Without the shift, a bar's own huge return (a breakout or a
    # crash) leaks into the rolling window used to price an option AT that
    # same bar, inflating vol right when it's least appropriate — e.g. a
    # sharp drop looks like it "shouldn't" hurt a long call much, because
    # vol spiked enough (from that very drop) to prop the price back up.
    vol_series = (realized_volatility(bars["close"], vol_window, bars_per_year) * iv_premium_multiplier).shift(1)

    result = OptionsBacktestResult(starting_equity=starting_equity)
    equity = starting_equity
    open_trade: OptionTrade | None = None

    for i in range(max(warmup, strategy.min_history, vol_window + 1), len(bars)):
        window = bars.iloc[: i + 1]
        now = window.index[-1]
        spot = float(window.iloc[-1]["close"])
        vol = vol_series.iloc[i]
        exited_this_bar = False

        if open_trade is not None:
            t_years = _time_to_expiry_years(now, open_trade.expiry)
            premium = bs_price(spot, open_trade.strike, t_years, rate, vol, open_trade.right)

            exit_reason = None
            if now.time() >= eod_flatten_time:
                exit_reason = "eod_flatten"
            elif premium <= open_trade.entry_premium * (1 - stop_loss_pct_of_premium):
                exit_reason = "stop_loss"
            elif profit_target_pct is not None and premium >= open_trade.entry_premium * (1 + profit_target_pct):
                exit_reason = "profit_target"
            elif exit_on_range_reentry:
                day_range = strategy.range_for_day(bars, now.date())
                if day_range is not None:
                    range_low, range_high = day_range
                    # Invalidated when price falls back below the level it
                    # broke out from — not all the way through the whole
                    # range to the far side.
                    if open_trade.right == "C" and spot <= range_high:
                        exit_reason = "range_reentry"
                    elif open_trade.right == "P" and spot >= range_low:
                        exit_reason = "range_reentry"

            if exit_reason is not None:
                open_trade.exit_time = now
                open_trade.exit_underlying = spot
                open_trade.exit_premium = premium
                open_trade.exit_reason = exit_reason
                equity += open_trade.pnl or 0.0
                result.trades.append(open_trade)
                open_trade = None
                exited_this_bar = True

        if open_trade is None and not exited_this_bar and pd.notna(vol):
            signal = strategy.generate_signal(symbol, window)
            if signal.action in ("BUY_CALL", "BUY_PUT"):
                right = "C" if signal.action == "BUY_CALL" else "P"
                direction = 1 if right == "C" else -1
                raw_strike = spot * (1 + direction * otm_pct)
                strike = round(raw_strike / strike_increment) * strike_increment
                expiry = (now.normalize() + timedelta(days=dte_days)).replace(
                    hour=16, minute=0, tzinfo=now.tzinfo
                )
                t_years = _time_to_expiry_years(now, expiry)
                entry_premium = bs_price(spot, strike, t_years, rate, vol, right)
                if entry_premium > 0.01:
                    contracts = int((equity * risk_pct_per_trade) // (entry_premium * CONTRACT_MULTIPLIER))
                    if contracts > 0:
                        open_trade = OptionTrade(
                            symbol, right, now, spot, strike, expiry, entry_premium, contracts,
                        )

        unrealized = 0.0
        if open_trade is not None:
            t_years = _time_to_expiry_years(now, open_trade.expiry)
            mark = bs_price(spot, open_trade.strike, t_years, rate, vol, open_trade.right)
            unrealized = (mark - open_trade.entry_premium) * open_trade.contracts * CONTRACT_MULTIPLIER
        result.equity_curve.append((now, equity + unrealized))

    if open_trade is not None:
        result.trades.append(open_trade)  # still open at end of data; unrealized only

    result.ending_equity = result.equity_curve[-1][1] if result.equity_curve else starting_equity
    return result
