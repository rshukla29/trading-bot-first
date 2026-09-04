from __future__ import annotations

import math
from dataclasses import dataclass, field


def position_size(equity: float, price: float, risk_pct: float, stop_loss_pct: float) -> int:
    """Number of shares to buy so that a hit on the stop-loss costs exactly
    `risk_pct` of account equity.

    shares = (equity * risk_pct) / (price * stop_loss_pct)
    """
    if price <= 0 or stop_loss_pct <= 0:
        return 0
    dollar_risk = equity * risk_pct
    risk_per_share = price * stop_loss_pct
    return max(0, math.floor(dollar_risk / risk_per_share))


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
