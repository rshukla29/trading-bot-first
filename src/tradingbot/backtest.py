from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from tradingbot.risk import position_size
from tradingbot.strategy.base import Strategy


@dataclass
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    quantity: int
    exit_date: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_reason: str | None = None

    @property
    def pnl(self) -> float | None:
        if self.exit_price is None:
            return None
        return (self.exit_price - self.entry_price) * self.quantity


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[pd.Timestamp, float]] = field(default_factory=list)
    starting_equity: float = 0.0
    ending_equity: float = 0.0

    @property
    def closed_trades(self) -> list[Trade]:
        return [t for t in self.trades if t.exit_price is not None]

    @property
    def win_rate(self) -> float:
        closed = self.closed_trades
        if not closed:
            return 0.0
        wins = sum(1 for t in closed if t.pnl and t.pnl > 0)
        return wins / len(closed)

    @property
    def total_return_pct(self) -> float:
        if self.starting_equity == 0:
            return 0.0
        return (self.ending_equity - self.starting_equity) / self.starting_equity

    @property
    def max_drawdown_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0][1]
        max_dd = 0.0
        for _, equity in self.equity_curve:
            peak = max(peak, equity)
            dd = (equity - peak) / peak if peak else 0.0
            max_dd = min(max_dd, dd)
        return max_dd


def run_backtest(
    symbol: str,
    bars: pd.DataFrame,
    strategy: Strategy,
    starting_equity: float,
    risk_pct: float,
    stop_loss_pct: float,
    warmup: int,
) -> BacktestResult:
    """Single-symbol backtest: walks the bar history day by day, entering on
    BUY signals (sized by risk-per-trade) and exiting on the strategy's EXIT
    signal or the 20% stop-loss, whichever comes first. No lookahead: each
    day's decision only sees bars up to and including that day.
    """
    result = BacktestResult(starting_equity=starting_equity)
    equity = starting_equity
    open_trade: Trade | None = None

    for i in range(warmup, len(bars)):
        window = bars.iloc[: i + 1]
        today = window.index[-1]
        row = window.iloc[-1]

        if open_trade is not None:
            stop_price = open_trade.entry_price * (1 - stop_loss_pct)
            if row["low"] <= stop_price:
                open_trade.exit_date = today
                open_trade.exit_price = stop_price
                open_trade.exit_reason = "stop_loss"
                equity += open_trade.pnl or 0.0
                result.trades.append(open_trade)
                open_trade = None
            else:
                signal = strategy.generate_signal(symbol, window)
                if signal.action == "EXIT":
                    open_trade.exit_date = today
                    open_trade.exit_price = float(row["close"])
                    open_trade.exit_reason = signal.reason
                    equity += open_trade.pnl or 0.0
                    result.trades.append(open_trade)
                    open_trade = None
        else:
            signal = strategy.generate_signal(symbol, window)
            if signal.action == "BUY":
                qty = position_size(equity, signal.price, risk_pct, stop_loss_pct)
                if qty > 0:
                    open_trade = Trade(symbol, today, signal.price, qty)

        mark_price = float(row["close"])
        unrealized = (mark_price - open_trade.entry_price) * open_trade.quantity if open_trade else 0.0
        result.equity_curve.append((today, equity + unrealized))

    if open_trade is not None:
        result.trades.append(open_trade)  # still open at end of data; unrealized only

    result.ending_equity = result.equity_curve[-1][1] if result.equity_curve else starting_equity
    return result
