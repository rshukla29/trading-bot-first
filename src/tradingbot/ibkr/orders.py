from __future__ import annotations

import logging

from ib_async import IB, MarketOrder, Stock, StopOrder, Trade

logger = logging.getLogger(__name__)


def place_bracket_buy(
    ib: IB,
    symbol: str,
    quantity: int,
    entry_price: float,
    stop_loss_pct: float,
) -> list[Trade]:
    """Place a market-entry buy with an attached stop-loss child order at
    entry_price * (1 - stop_loss_pct). The parent is held (transmit=False)
    until the stop-loss child (transmit=True) is placed, so both submit
    together as a linked pair. Returns [parent_trade, stop_trade].
    """
    if quantity <= 0:
        raise ValueError("quantity must be positive")

    contract = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(contract)

    stop_price = round(entry_price * (1 - stop_loss_pct), 2)

    parent = MarketOrder("BUY", quantity)
    parent.orderId = ib.client.getReqId()
    parent.transmit = False

    stop_loss = StopOrder("SELL", quantity, stop_price)
    stop_loss.orderId = ib.client.getReqId()
    stop_loss.parentId = parent.orderId
    stop_loss.transmit = True

    parent_trade = ib.placeOrder(contract, parent)
    stop_trade = ib.placeOrder(contract, stop_loss)

    logger.info("Placed bracket BUY %s x%d, stop @ %.2f", symbol, quantity, stop_price)
    return [parent_trade, stop_trade]


def place_market_sell(ib: IB, symbol: str, quantity: int) -> Trade:
    contract = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(contract)
    order = MarketOrder("SELL", quantity)
    trade = ib.placeOrder(contract, order)
    logger.info("Placed market SELL %s x%d", symbol, quantity)
    return trade
