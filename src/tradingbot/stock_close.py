"""Close a daily stock bot's own positions at market: the dashboard's
"Stop and close positions at market" for RSI(2) and MA crossover.

Only symbols the strategy claimed in the journal's stock_ownership table
are sold, never a manual position or another strategy's. Run as client 1
(IBKR_CLIENT_ID): place_market_sell cancels each symbol's protective stop
first, and only the client that placed a stop (client 1) can cancel it.
During market hours that's run_step_stop_bot; after 4pm, the 4:30pm
RSI(2) / MA-crossover run itself.
"""
from __future__ import annotations

import logging

from tradingbot.ibkr.orders import place_market_sell
from tradingbot.journal import Journal

logger = logging.getLogger(__name__)


def close_owned(ib, journal: Journal, strategy: str, held: dict[str, float]) -> list[str]:
    """Market-sell every long stock position `strategy` owns. `held`:
    symbol -> shares held at IBKR. Returns the symbols sold."""
    sold = []
    for symbol in sorted(journal.owned_stocks(strategy)):
        qty = int(held.get(symbol, 0))
        if qty <= 0:
            continue
        if place_market_sell(ib, symbol, qty) is None:
            continue
        journal.log_order(symbol, "SELL", qty, "MKT", broker="IBKR", status="submitted")
        logger.warning("%s: closing %s x%d at market (stopped from the dashboard)", strategy, symbol, qty)
        sold.append(symbol)
    return sold
