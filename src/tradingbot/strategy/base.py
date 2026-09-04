from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import pandas as pd

Action = Literal["BUY", "EXIT", "HOLD"]


@dataclass(frozen=True)
class Signal:
    symbol: str
    action: Action
    price: float
    timestamp: datetime
    reason: str


class Strategy(ABC):
    """Interface every strategy implements: turn a bar history into today's signal."""

    @property
    @abstractmethod
    def min_history(self) -> int:
        """Minimum number of bars needed before this strategy can emit a
        non-HOLD signal. Callers use it to size backtest warmup and to
        request enough history from the broker."""
        raise NotImplementedError

    @abstractmethod
    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        """bars must be a DataFrame indexed by date with an ascending-sorted
        'close' column (and typically open/high/low/volume). Returns the
        signal for the most recent bar only."""
        raise NotImplementedError
