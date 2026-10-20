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
from tradingbot.risk import step_trailing_stop

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


def plan_stop_raise(
    avg_cost: float, price: float, current_stop: float, step_pct: float, buffer_pct: float,
    max_loss_pct: float | None = None,
) -> float | None:
    """New (higher) stop level from the 10% step rule, or None to leave
    the stop alone. Never returns a level at or below the current stop.

    max_loss_pct: also lift a stop that sits further below the real fill
    cost than the documented max loss. Bracket stops are priced off the
    signal close, but the entry fills at the next open — 2026-10-20: LLY
    cost 1265.93 with its stop at 1116.31 (-11.8%), INTC -14.2%. Skipped
    while the price is within the buffer of that floor, so lifting the stop
    can't trigger it on the spot."""
    candidates = [step_trailing_stop(avg_cost, price, step_pct, buffer_pct)]
    if max_loss_pct:
        floor = round(avg_cost * (1 - max_loss_pct), 2)
        if price > floor * (1 + buffer_pct):
            candidates.append(floor)
    level = max((c for c in candidates if c is not None), default=None)
    if level is None or level <= current_stop:
        return None
    return level


def symbols_with_pending_exit(trades) -> set[str]:
    """Stock symbols with a working non-stop SELL order — an exit already
    in progress (e.g. RSI(2)'s after-close market sell waiting for the
    open). Such a position must not get a new stop: when the sell fills,
    the stop would be left on a flat position and could open a short."""
    return {
        t.contract.symbol for t in trades
        if t.contract.secType == "STK" and t.order.action == "SELL" and t.order.orderType != "STP"
    }


def orphan_stops(trades, held_symbols: set[str]) -> list:
    """Working SELL stop orders on stocks that are no longer held. Left in
    place, one would sell shares that aren't there if price falls to it,
    opening a short (found 2026-09-28: BAC sold at the open while the
    stop audit placed a fresh stop one second later).

    A stop on a symbol with a working BUY is NOT an orphan: that's a new
    bracket entry (placed after the close, fills at the next open) whose
    stop legitimately exists before the position does."""
    buying = {t.contract.symbol for t in trades if t.contract.secType == "STK" and t.order.action == "BUY"}
    return [
        t for t in trades
        if t.contract.secType == "STK" and t.order.action == "SELL" and t.order.orderType == "STP"
        and t.contract.symbol not in held_symbols and t.contract.symbol not in buying
    ]


def cancel_orphan_stops(ib: IB, held_symbols: set[str]) -> list[str]:
    """Cancel this client's stops on flat positions. Only this client's
    orders (reqOpenOrders): another client's orders can't be cancelled from
    here anyway, and manual TWS orders are left to the user."""
    cancelled = []
    for t in orphan_stops(ib.reqOpenOrders(), held_symbols):
        logger.warning(
            "%s: cancelling orphan stop (SELL %d @ %.2f) — no position left to protect",
            t.contract.symbol, t.order.totalQuantity, t.order.auxPrice,
        )
        ib.cancelOrder(t.order)
        cancelled.append(t.contract.symbol)
    if cancelled:
        ib.sleep(1)
    return cancelled


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
    exiting = symbols_with_pending_exit(ib.reqAllOpenOrders())
    sold: set[str] = set()
    for symbol, pos in positions.items():
        qty = int(pos.position)
        if symbol in exiting:
            logger.info("%s: exit order already working — no stop needed", symbol)
            continue
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
