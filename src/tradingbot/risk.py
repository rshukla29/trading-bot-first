from __future__ import annotations

import math
from dataclasses import dataclass, field


def position_size(
    equity: float, price: float, risk_pct: float, stop_loss_pct: float, max_position_value: float | None = None,
) -> int:
    """Number of shares to buy so that a hit on the stop-loss costs exactly
    `risk_pct` of account equity, further capped so the position's notional
    (shares * price) never exceeds `max_position_value`.

    shares = (equity * risk_pct) / (price * stop_loss_pct)
    """
    if price <= 0 or stop_loss_pct <= 0:
        return 0
    dollar_risk = equity * risk_pct
    risk_per_share = price * stop_loss_pct
    qty = max(0, math.floor(dollar_risk / risk_per_share))
    if max_position_value is not None:
        qty = min(qty, math.floor(max_position_value / price))
    return qty


def step_trailing_stop(entry: float, price: float, step_pct: float = 0.10, buffer_pct: float = 0.01) -> float | None:
    """Profit-locking stop level in fixed steps: once the gain reaches
    step + buffer, the stop moves to entry + step; at 2*step + buffer to
    entry + 2*step; and so on. With the defaults, bought at $10:
    +11% ($11.10) -> stop $11.00, +21% ($12.10) -> stop $12.00.

    Returns None below the first step (+11%) — the original stop stays in
    force. Callers must only ever *raise* a stop with this (take the max
    with the current stop), never lower it. The buffer keeps the new stop
    from landing right at the current price and firing on the next tick.
    """
    if entry <= 0 or step_pct <= 0:
        return None
    gain = price / entry - 1
    # epsilon: 0.11 - 0.01 = 0.0999999... in floating point
    steps = math.floor((gain - buffer_pct) / step_pct + 1e-9)
    if steps < 1:
        return None
    return round(entry * (1 + steps * step_pct), 2)


@dataclass
class DailyLossKillSwitch:
    """Halts new trading for the day once realized losses exceed a threshold."""

    max_daily_loss_pct: float
    starting_equity: float
    realized_pnl_today: float = field(default=0.0)

    def record_pnl(self, pnl: float) -> None:
        self.realized_pnl_today += pnl

    def is_tripped(self) -> bool:
        loss_limit = -abs(self.max_daily_loss_pct) * self.starting_equity
        return self.realized_pnl_today <= loss_limit

    def reset(self, starting_equity: float) -> None:
        self.starting_equity = starting_equity
        self.realized_pnl_today = 0.0


def can_open_new_position(current_open_positions: int, max_positions: int) -> bool:
    return current_open_positions < max_positions
