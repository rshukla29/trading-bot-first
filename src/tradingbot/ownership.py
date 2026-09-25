"""Position ownership for the daily stock bots (MA-crossover, RSI(2)).

A bot only manages (EXITs, counts toward max_positions) stock positions it
opened itself, recorded in the journal's stock_ownership table when it
places a BUY. Any other held stock — bought manually, or by another
strategy — is left completely alone: no EXIT, and no new BUY on top of it
(IBKR merges shares into one position, so a later bot EXIT would sell the
manual shares too).
"""
from __future__ import annotations

from dataclasses import dataclass

from tradingbot.journal import Journal


@dataclass(frozen=True)
class Ownership:
    mine: set[str]       # held and opened by this bot: manage these
    not_mine: set[str]   # held but not opened by this bot: never touch


def resolve_ownership(
    journal: Journal, strategy: str, held_symbols: set[str], symbols_with_open_orders: set[str],
) -> Ownership:
    """Split held stock symbols into this bot's and everyone else's, and
    drop stale claims — a claimed symbol that's no longer held and has no
    working orders (stopped out or sold). A claim whose BUY is still
    pending (placed after the close, fills at the next open) has an open
    order, so it's kept."""
    claimed = journal.owned_stocks(strategy)
    for symbol in claimed - held_symbols - symbols_with_open_orders:
        journal.release_stock(symbol)
    mine = claimed & held_symbols
    return Ownership(mine=mine, not_mine=held_symbols - mine)
