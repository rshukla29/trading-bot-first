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
    entry_reason: str = ""
    exit_date: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_reason: str | None = None

    @property
    def entry_source(self) -> str:
        return "consolidation retest" if "consolidation" in self.entry_reason else "MA crossover"

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
    signal or the configured stop-loss, whichever comes first. No lookahead: each
    day's decision only sees bars up to and including that day.

    Signal-based fills (BUY entries and EXIT-signal exits) execute at the
    *next* bar's open, not the signal bar's own close. A daily-bar signal
    can only be computed once that bar is final — which, live, means after
    the exchange has already closed for the day (see run_rsi2_bot.py /
    run_ibkr_bot.py's 4:30pm ET schedule) — so "buy at the same bar's
    close" was never actually achievable; the earliest realistic fill is
    the next session's open. Stop-loss exits are different and stay
    same-bar: they're a resting broker-side order (see orders.py's
    place_bracket_buy) that can trigger intraday the moment the bar's low
    touches it, no next-day delay involved.
    """
    result = BacktestResult(starting_equity=starting_equity)
    equity = starting_equity
    open_trade: Trade | None = None
    pending_entry_reason: str | None = None
    pending_exit_reason: str | None = None

    for i in range(warmup, len(bars)):
        window = bars.iloc[: i + 1]
        today = window.index[-1]
        row = window.iloc[-1]
        today_open = float(row["open"])

        # Fill anything queued from yesterday's signal, at today's open.
        if pending_exit_reason is not None and open_trade is not None:
            open_trade.exit_date = today
            open_trade.exit_price = today_open
            open_trade.exit_reason = pending_exit_reason
            equity += open_trade.pnl or 0.0
            result.trades.append(open_trade)
            open_trade = None
            pending_exit_reason = None

        if pending_entry_reason is not None and open_trade is None:
            qty = position_size(equity, today_open, risk_pct, stop_loss_pct)
            if qty > 0:
                open_trade = Trade(symbol, today, today_open, qty, entry_reason=pending_entry_reason)
            pending_entry_reason = None

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
                    pending_exit_reason = signal.reason
        else:
            signal = strategy.generate_signal(symbol, window)
            if signal.action == "BUY":
                pending_entry_reason = signal.reason

        mark_price = float(row["close"])
        unrealized = (mark_price - open_trade.entry_price) * open_trade.quantity if open_trade else 0.0
        result.equity_curve.append((today, equity + unrealized))

    if open_trade is not None:
        result.trades.append(open_trade)  # still open at end of data; unrealized only

    result.ending_equity = result.equity_curve[-1][1] if result.equity_curve else starting_equity
    return result
