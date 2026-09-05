from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from tradingbot.options.pricing import bs_price, find_strike_for_delta, realized_volatility, vol_percentile

TRADING_DAYS_PER_YEAR = 252
CONTRACT_MULTIPLIER = 100


@dataclass
class IronCondorConfig:
    """Parameters grounded in published options-selling research (see
    commit history / conversation for citations): 16-delta short strikes,
    45 DTE, managed at 50% profit target and a 100%-of-credit stop-loss,
    forced flat by 21 DTE to avoid late-cycle gamma risk.
    """

    target_delta: float = 0.16
    wing_width: float = 5.0
    dte_days: int = 45
    profit_target_pct: float = 0.50
    stop_loss_multiple: float = 2.0  # close when cost-to-close >= credit * this
    dte_forced_close: int = 21
    entry_interval_days: int = 7
    max_concurrent_positions: int = 3
    strike_increment: float = 1.0
    vol_window: int = 20
    iv_premium_multiplier: float = 1.2
    rate: float = 0.04
    min_vol_percentile: float | None = None  # e.g. 0.5 = only sell when vol is in its own top half
    vol_percentile_lookback: int = 252


@dataclass
class IronCondorTrade:
    symbol: str
    entry_date: pd.Timestamp
    expiry: pd.Timestamp
    call_short_strike: float
    call_long_strike: float
    put_short_strike: float
    put_long_strike: float
    credit_received: float  # per share, both spreads combined
    max_loss_per_contract: float  # dollars, one contract
    contracts: int
    exit_date: pd.Timestamp | None = None
    exit_cost: float | None = None  # per share, cost to close both spreads
    exit_reason: str | None = None

    @property
    def pnl(self) -> float | None:
        if self.exit_cost is None:
            return None
        return (self.credit_received - self.exit_cost) * self.contracts * CONTRACT_MULTIPLIER


@dataclass
class IronCondorBacktestResult:
    trades: list[IronCondorTrade] = field(default_factory=list)
    equity_curve: list[tuple[pd.Timestamp, float]] = field(default_factory=list)
    starting_equity: float = 0.0
    ending_equity: float = 0.0

    @property
    def closed_trades(self) -> list[IronCondorTrade]:
        return [t for t in self.trades if t.exit_cost is not None]

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


def _condor_cost_to_close(spot: float, t_years: float, rate: float, vol: float, trade: IronCondorTrade) -> float:
    """Per-share cost to buy back both spreads (what you'd pay to exit now)."""
    call_cost = bs_price(spot, trade.call_short_strike, t_years, rate, vol, "C") - bs_price(
        spot, trade.call_long_strike, t_years, rate, vol, "C"
    )
    put_cost = bs_price(spot, trade.put_short_strike, t_years, rate, vol, "P") - bs_price(
        spot, trade.put_long_strike, t_years, rate, vol, "P"
    )
    return call_cost + put_cost


def run_iron_condor_backtest(
    symbol: str,
    bars: pd.DataFrame,
    config: IronCondorConfig,
    starting_equity: float,
    risk_pct_per_trade: float,
    warmup: int = 0,
) -> IronCondorBacktestResult:
    """Daily-bar iron condor backtest: opens a new 45-DTE condor every
    entry_interval_days (up to max_concurrent_positions at once), managed
    at 50% profit / 100%-of-credit stop / forced-closed at 21 DTE.

    Same Black-Scholes-simulation caveat as the ORB backtest: this account
    has no real historical options data, so strikes are selected by
    inverting BS delta and premiums are BS prices off realized volatility
    (lagged one bar — see options/backtest.py for why that lag matters).
    """
    bars_per_year = TRADING_DAYS_PER_YEAR
    vol_series = (realized_volatility(bars["close"], config.vol_window, bars_per_year) * config.iv_premium_multiplier).shift(1)
    vol_pct_series = None
    if config.min_vol_percentile is not None:
        vol_pct_series = vol_percentile(vol_series, config.vol_percentile_lookback)

    min_start = max(warmup, config.vol_window + 2)
    if config.min_vol_percentile is not None:
        min_start = max(min_start, config.vol_percentile_lookback + config.vol_window + 2)

    result = IronCondorBacktestResult(starting_equity=starting_equity)
    equity = starting_equity
    open_positions: list[IronCondorTrade] = []
    last_entry_index: int | None = None

    for i in range(min_start, len(bars)):
        now = bars.index[i]
        spot = float(bars["close"].iloc[i])
        vol = vol_series.iloc[i]

        still_open = []
        for trade in open_positions:
            t_years = max(0.0, (trade.expiry - now).days / 365.25)
            cost = _condor_cost_to_close(spot, t_years, config.rate, vol, trade)
            days_to_expiry = (trade.expiry - now).days

            exit_reason = None
            if cost <= trade.credit_received * (1 - config.profit_target_pct):
                exit_reason = "profit_target"
            elif cost >= trade.credit_received * config.stop_loss_multiple:
                exit_reason = "stop_loss"
            elif days_to_expiry <= config.dte_forced_close:
                exit_reason = "dte_forced_close"

            if exit_reason is not None:
                trade.exit_date = now
                trade.exit_cost = cost
                trade.exit_reason = exit_reason
                equity += trade.pnl or 0.0
                result.trades.append(trade)
            else:
                still_open.append(trade)
        open_positions = still_open

        can_enter = (
            len(open_positions) < config.max_concurrent_positions
            and pd.notna(vol)
            and (last_entry_index is None or (i - last_entry_index) >= config.entry_interval_days)
        )
        if can_enter and vol_pct_series is not None:
            pct = vol_pct_series.iloc[i]
            can_enter = pd.notna(pct) and pct >= config.min_vol_percentile

        if can_enter:
            expiry = now + pd.Timedelta(days=config.dte_days)
            t_years = config.dte_days / 365.25

            call_short = find_strike_for_delta(spot, t_years, config.rate, vol, "C", config.target_delta, config.strike_increment)
            call_long = call_short + config.wing_width
            put_short = find_strike_for_delta(spot, t_years, config.rate, vol, "P", config.target_delta, config.strike_increment)
            put_long = put_short - config.wing_width

            credit = (
                bs_price(spot, call_short, t_years, config.rate, vol, "C") - bs_price(spot, call_long, t_years, config.rate, vol, "C")
            ) + (
                bs_price(spot, put_short, t_years, config.rate, vol, "P") - bs_price(spot, put_long, t_years, config.rate, vol, "P")
            )
            max_loss_per_contract = config.wing_width * CONTRACT_MULTIPLIER - credit * CONTRACT_MULTIPLIER

            if max_loss_per_contract > 1.0 and credit > 0.01:
                contracts = int((equity * risk_pct_per_trade) // max_loss_per_contract)
                if contracts > 0:
                    open_positions.append(
                        IronCondorTrade(
                            symbol, now, expiry, call_short, call_long, put_short, put_long,
                            credit, max_loss_per_contract, contracts,
                        )
                    )
                    last_entry_index = i

        unrealized = 0.0
        for trade in open_positions:
            t_years = max(0.0, (trade.expiry - now).days / 365.25)
            cost = _condor_cost_to_close(spot, t_years, config.rate, vol, trade)
            unrealized += (trade.credit_received - cost) * trade.contracts * CONTRACT_MULTIPLIER
        result.equity_curve.append((now, equity + unrealized))

    result.trades.extend(open_positions)  # still open at end of data; unrealized only
    result.ending_equity = result.equity_curve[-1][1] if result.equity_curve else starting_equity
    return result
