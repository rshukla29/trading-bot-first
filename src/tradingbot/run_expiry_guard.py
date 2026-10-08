"""3:57pm safety net: close every option position that expires today.

The ORB bot flattens its same-day options at 3:55pm, but only while its
process is alive. If it has crashed, an expiring equity/ETF option is
auto-exercised at the close when in the money — SPY and IWM options settle
in shares, so 10 ITM contracts would become 1,000 shares (~$280k of IWM)
overnight, far beyond any position limit. Out of the money, it just
expires worthless. This task runs independently of every bot, finds any
option (long or short, from any strategy) whose expiry is today, and
closes it with a market order before 4:00pm.

Double-close protection: if a working order already exists for a
contract (e.g. ORB's own 3:55 flatten still filling), the guard waits for
it to finish and re-reads the position before doing anything — selling on
top of a pending sell would leave a short option.

Normally a no-op: on a day ORB flattened as designed, there's nothing to
close. Any ORB journal row it closes is marked exit_reason="expiry_guard".

    python -m tradingbot.run_expiry_guard
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from ib_async import IB, MarketOrder

from tradingbot.config import settings
from tradingbot.ibkr.client import IBKRClient
from tradingbot.journal import Journal
from tradingbot.options.live_orb import CONTRACT_MULTIPLIER, wait_for_fill

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("ib_async").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
PENDING_WAIT_SECONDS = 60


def expiring_today(positions, today: date) -> list:
    """Option positions (nonzero) whose expiry is `today`."""
    stamp = today.strftime("%Y%m%d")
    return [
        p for p in positions
        if p.contract.secType == "OPT" and p.position != 0 and p.contract.lastTradeDateOrContractMonth == stamp
    ]


def closing_order(position) -> MarketOrder:
    """SELL a long, BUY back a short — always the full quantity."""
    action = "SELL" if position.position > 0 else "BUY"
    return MarketOrder(action, abs(position.position), tif="DAY")


def _working_orders_for(ib: IB, con_id: int) -> list:
    return [t for t in ib.reqAllOpenOrders() if t.contract.conId == con_id]


def _current_position(ib: IB, con_id: int) -> float:
    return next((p.position for p in ib.positions() if p.contract.conId == con_id), 0.0)


def long_is_worthless(ib: IB, contract) -> bool:
    """A long option nobody bids for can't be sold and expires worthless —
    no exercise risk (an in-the-money option always has a bid)."""
    from tradingbot.options.live_chain import get_quote_lenient
    contract.exchange = contract.exchange or "SMART"
    try:
        bid, _, _ = get_quote_lenient(ib, contract)
    except ValueError:
        return False  # no quote at all: don't assume, try to close
    return bid == 0


def _close_journal_rows(journal: Journal, con_id: int, fill_price: float) -> None:
    for row in journal.open_orb_positions_by_conid(con_id):
        pnl = (fill_price - row["entry_premium"]) * row["contracts"] * CONTRACT_MULTIPLIER
        journal.close_orb_position(row["id"], datetime.now(timezone.utc).isoformat(), fill_price, "expiry_guard", pnl)
        logger.warning("Journal: closed ORB position id=%s (expiry_guard), pnl=%.2f", row["id"], pnl)
    # Short leg of an ORB credit spread bought back by the guard: record the
    # spread's result (long leg assumed worthless — it's left to expire).
    for row in journal.open_credit_orb_rows():
        if row["short_conid"] == con_id:
            pnl = (row["credit"] - fill_price) * row["contracts"] * CONTRACT_MULTIPLIER
            journal.close_credit_orb(row["id"], fill_price, "expiry_guard", pnl)
            logger.warning("Journal: closed credit spread id=%s (expiry_guard), pnl=%.2f", row["id"], pnl)


def main() -> None:
    today = datetime.now(ET).date()
    journal = Journal(settings.db_path)
    with IBKRClient(settings, client_id=settings.expiry_guard_client_id) as client:
        ib = client.ib
        targets = expiring_today(ib.positions(), today)
        if not targets:
            logger.info("No option positions expiring today — nothing to do")
            return

        # Shorts first: they carry the assignment risk.
        for pos in sorted(targets, key=lambda p: p.position):
            c = pos.contract
            if _working_orders_for(ib, c.conId):
                logger.info("%s: order already working (bot exit in progress) — waiting up to %ds",
                            c.localSymbol, PENDING_WAIT_SECONDS)
                waited = 0
                while waited < PENDING_WAIT_SECONDS and _working_orders_for(ib, c.conId):
                    ib.sleep(5)
                    waited += 5
                if _working_orders_for(ib, c.conId):
                    logger.error("%s: another order still working after %ds — not adding a second order; "
                                 "CHECK MANUALLY before 4:00pm", c.localSymbol, PENDING_WAIT_SECONDS)
                    continue

            qty = _current_position(ib, c.conId)
            if qty == 0:
                logger.info("%s: already flat", c.localSymbol)
                continue

            pos_now = next(p for p in ib.positions() if p.contract.conId == c.conId)
            if pos_now.position > 0 and long_is_worthless(ib, c):
                logger.info("%s: long option with no bid — worthless, left to expire", c.localSymbol)
                continue
            order = closing_order(pos_now)
            logger.warning("%s: STILL OPEN at %s with expiry today — %s %d at market",
                           c.localSymbol, datetime.now(ET).strftime("%H:%M:%S"), order.action, order.totalQuantity)
            c.exchange = c.exchange or "SMART"
            trade = ib.placeOrder(c, order)
            if wait_for_fill(ib, trade, timeout=90):
                fill = trade.orderStatus.avgFillPrice
                logger.info("%s: closed %d @ %.2f", c.localSymbol, order.totalQuantity, fill)
                journal.log_order(c.symbol, f"{order.action}_EXPIRY_GUARD", int(order.totalQuantity), "OPT",
                                  broker="IBKR", status="filled")
                _close_journal_rows(journal, c.conId, fill)
            else:
                logger.error("%s: close did NOT fill (status=%s) — CHECK MANUALLY before 4:00pm",
                             c.localSymbol, trade.orderStatus.status)


if __name__ == "__main__":
    main()
