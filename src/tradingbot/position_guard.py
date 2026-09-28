"""No unintended short positions.

2026-09-28: ORB's exit sold 10 contracts at the mid; when the order only
partly filled before its 45s timeout it was cancelled and the next tick
sold all 10 again, ignoring the partial fill. Result: short 11 IWM and 2
SPY same-day puts. The only shorts this account should ever hold are the
iron condor's two sold legs (paired with bought wings).

Three layers, used by every bot:
  1. clamp_sell_qty: a sell never exceeds what IBKR shows as held.
  2. Callers account for partial fills (see run_orb_bot / run_trend_join_bot).
  3. unexpected_shorts: detects any short that isn't a condor leg, so it can
     be bought back (ORB does this every tick for its symbols) and flagged
     in the daily snapshot.
"""
from __future__ import annotations

import logging

from ib_async import IB, MarketOrder

from tradingbot.journal import Journal

logger = logging.getLogger(__name__)


def held_qty(ib: IB, con_id: int) -> float:
    """Current IBKR position for a contract (0 if none)."""
    return next((p.position for p in ib.positions() if p.contract.conId == con_id), 0.0)


def clamp_sell_qty(wanted: float, held: float) -> int:
    """Largest sell that can't flip a long into a short."""
    return max(0, int(min(wanted, held)))


def intended_short_conids(journal: Journal) -> set[int]:
    """Contracts that are meant to be short: open iron condors' sold legs."""
    conids: set[int] = set()
    for row in journal.open_condor_positions():
        conids.add(row["call_short_conid"])
        conids.add(row["put_short_conid"])
    return conids


def unexpected_shorts(positions, allowed_conids: set[int]) -> list:
    """Every negative position that isn't an intended condor short leg."""
    return [p for p in positions if p.position < 0 and p.contract.conId not in allowed_conids]


def buy_back(ib: IB, position) -> object:
    """Market BUY to flatten an unintended short."""
    contract = position.contract
    contract.exchange = contract.exchange or "SMART"
    qty = abs(position.position)
    logger.error(
        "UNINTENDED SHORT %s x%d — buying back at market",
        contract.localSymbol or contract.symbol, qty,
    )
    return ib.placeOrder(contract, MarketOrder("BUY", qty, tif="DAY"))
