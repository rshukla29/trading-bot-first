from __future__ import annotations

import logging

from ib_async import IB, MarketOrder, Stock, StopOrder, Trade

logger = logging.getLogger(__name__)


def await_ack(ib: IB, trades: list, timeout: float = 5.0) -> bool:
    """Wait until IBKR has acknowledged every order (status no longer
    PendingSubmit). 2026-10-08: RSI(2) placed an INTC bracket and
    disconnected 0.2s later, before IBKR acknowledged it — the order was
    lost. Returns False (and logs an error) if any order is still
    unacknowledged after `timeout` seconds."""
    waited = 0.0
    while waited < timeout:
        if all(t.orderStatus.status not in ("PendingSubmit", "") for t in trades):
            return True
        ib.sleep(0.5)
        waited += 0.5
    for t in trades:
        if t.orderStatus.status in ("PendingSubmit", ""):
            logger.error("Order not acknowledged by IBKR after %.0fs: %s %s x%s %s", timeout,
                         t.order.action, t.contract.symbol, t.order.totalQuantity, t.order.orderType)
    return False


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

    parent = MarketOrder("BUY", quantity, tif="DAY")
    parent.orderId = ib.client.getReqId()
    parent.transmit = False

    # GTC, not the default DAY: these are multi-day swing positions, and a
    # DAY stop silently expired at the close of the entry day — leaving
    # every held position unprotected from day two on (found 2026-09-25:
    # zero working orders while holding three bot positions).
    stop_loss = StopOrder("SELL", quantity, stop_price, tif="GTC")
    stop_loss.orderId = ib.client.getReqId()
    stop_loss.parentId = parent.orderId
    stop_loss.transmit = True

    parent_trade = ib.placeOrder(contract, parent)
    stop_trade = ib.placeOrder(contract, stop_loss)

    acked = await_ack(ib, [parent_trade, stop_trade])
    logger.info("Placed bracket BUY %s x%d, stop @ %.2f%s", symbol, quantity, stop_price,
                "" if acked else " — NOT ACKNOWLEDGED, check manually")
    return [parent_trade, stop_trade]


def cancel_stop_orders(ib: IB, symbol: str) -> int:
    """Cancel this client's working SELL stop orders for `symbol`. Only the
    client ID that placed an order can cancel it (IBKR error 10147), which
    is why the stock bots keep a fixed client ID."""
    cancelled = 0
    for trade in ib.reqOpenOrders():
        order = trade.order
        if trade.contract.symbol == symbol and order.action == "SELL" and order.orderType == "STP":
            ib.cancelOrder(order)
            cancelled += 1
    if cancelled:
        ib.sleep(1)
        logger.info("Cancelled %d protective stop(s) for %s", cancelled, symbol)
    return cancelled


def place_market_sell(ib: IB, symbol: str, quantity: int) -> Trade | None:
    """Exit a position. Cancels its GTC protective stop first — left
    working, that stop could later fire on a flat position and open a
    short."""
    contract = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(contract)
    held = next((p.position for p in ib.positions() if p.contract.symbol == symbol and p.contract.secType == "STK"), 0)
    quantity = max(0, int(min(quantity, held)))  # never sell into a short
    if quantity == 0:
        logger.warning("Not selling %s: nothing held at IBKR", symbol)
        return None
    cancel_stop_orders(ib, symbol)
    # Explicit tif: left unset, the account's order preset rewrites it to
    # DAY via error 10349 and a cancel/resubmit cycle (seen 2026-09-25).
    order = MarketOrder("SELL", quantity, tif="DAY")
    trade = ib.placeOrder(contract, order)
    acked = await_ack(ib, [trade])
    logger.info("Placed market SELL %s x%d%s", symbol, quantity, "" if acked else " — NOT ACKNOWLEDGED, check manually")
    return trade
