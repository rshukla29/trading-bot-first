"""Stop-loss audit for the daily stock bots' own positions.

Background (2026-09-25): the bracket stops were placed without a tif, so
IBKR's order preset made them DAY orders that expired at the close of the
entry day. Three positions sat unprotected for days, and CBIO fell ~26%
through its 10% stop before anyone noticed. Making the stops GTC fixed the
cause; this audit makes sure a missing stop can't go unnoticed again,
whatever the cause (expired, manually cancelled, partially filled...).

Every MA-crossover / RSI(2) run checks each position it owns, before
anything else:
  - working SELL stops cover the full position  -> fine
  - no (or partial) cover, price above the 10% level -> place a GTC stop
  - no (or partial) cover, price at/below the level  -> sell now (the stop
    would already have fired)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from ib_async import IB, Stock, StopOrder

from tradingbot.ibkr.data import fetch_daily_bars
from tradingbot.ibkr.orders import cancel_stop_orders, place_market_sell
from tradingbot.journal import Journal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StopCheck:
    action: Literal["ok", "place_stop", "sell_now"]
    stop_price: float


def check_protection(
    avg_cost: float, last_price: float, position_qty: float, working_stop_qty: float, stop_loss_pct: float,
) -> StopCheck:
    stop_price = round(avg_cost * (1 - stop_loss_pct), 2)
    if working_stop_qty >= position_qty:
        return StopCheck("ok", stop_price)
    if last_price <= stop_price:
        return StopCheck("sell_now", stop_price)
    return StopCheck("place_stop", stop_price)


def working_stop_qty_by_symbol(trades) -> dict[str, float]:
    """Total quantity of working SELL stop orders per stock symbol."""
    qty: dict[str, float] = {}
    for t in trades:
        o = t.order
        if t.contract.secType == "STK" and o.action == "SELL" and o.orderType == "STP":
            qty[t.contract.symbol] = qty.get(t.contract.symbol, 0.0) + o.totalQuantity
    return qty


def ensure_protective_stops(
    ib: IB, journal: Journal, positions: dict, stop_loss_pct: float, source: str,
) -> set[str]:
    """Audit `positions` ({symbol: ib_async Position}, this bot's own only).
    Returns the symbols sold because they were already through their stop
    — the caller must stop treating those as open."""
    stops = working_stop_qty_by_symbol(ib.reqOpenOrders())
    sold: set[str] = set()
    for symbol, pos in positions.items():
        qty = int(pos.position)
        try:
            last = float(fetch_daily_bars(ib, symbol, duration="5 D")["close"].iloc[-1])
        except Exception:
            logger.exception("%s: stop audit couldn't get a price — placing stop without the price check", symbol)
            last = float("inf")
        check = check_protection(pos.avgCost, last, qty, stops.get(symbol, 0.0), stop_loss_pct)

        if check.action == "ok":
            continue
        if check.action == "sell_now":
            logger.warning(
                "%s: UNPROTECTED and already through its stop (last %.2f <= %.2f) — selling %d",
                symbol, last, check.stop_price, qty,
            )
            place_market_sell(ib, symbol, qty)
            journal.log_order(symbol, "SELL", qty, "MKT", broker="IBKR", status="submitted")
            journal.log_signal(symbol, "EXIT", last, f"stop audit: below {check.stop_price} with no working stop", source)
            sold.add(symbol)
            continue

        logger.warning("%s: UNPROTECTED — placing GTC stop %d @ %.2f", symbol, qty, check.stop_price)
        cancel_stop_orders(ib, symbol)  # replace any partial-quantity stop with one full-size stop
        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)
        ib.placeOrder(contract, StopOrder("SELL", qty, check.stop_price, tif="GTC"))
        journal.log_order(symbol, "SELL", qty, "STP", broker="IBKR", status="submitted", stop_price=check.stop_price)
    return sold
