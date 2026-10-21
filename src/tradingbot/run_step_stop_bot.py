"""Intraday stop manager for the daily stock bots' positions (MA-crossover,
RSI(2)): raises each position's broker-side stop in 10% steps as it moves
into profit (see risk.step_trailing_stop). Bought at $10: +11% -> stop
$11.00, +21% -> $12.00, and so on. Stops are only ever raised.

Runs 9:30am-4:00pm ET, checking every ~60s, so an intraday spike that
fades before the close still locks in its step. At startup it also runs
the stop audit (protection.ensure_protective_stops) so every owned
position has a working stop before the session starts.

Manual positions and other strategies' positions are never touched (only
symbols claimed in the journal's stock_ownership table). Trend Join
(own trailing rules, flat by 15:51) and the options strategies aren't
covered here; ORB applies the same step rule to option premium itself.

Must use IBKR_CLIENT_ID (1): IBKR only lets the client that placed an
order modify it, and the stock bots place their stops as client 1. Safe
because this process exits at 4:00pm and the client-1 bots run at 4:30pm —
but don't start run_rsi2_bot / run_ibkr_bot manually while this is
running (IBKR error 326, client id already in use).

    python -m tradingbot.run_step_stop_bot
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, time
from zoneinfo import ZoneInfo

from ib_async import Stock

from tradingbot.config import settings
from tradingbot.ibkr.client import IBKRClient
from tradingbot.journal import Journal
from tradingbot.protection import cancel_orphan_stops, ensure_protective_stops, plan_stop_raise

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("ib_async").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
TICK_SECONDS = 60.0
STRATEGIES = ("ma_crossover", "rsi2")
SOURCE = "run_step_stop_bot"


def _now_et() -> datetime:
    return datetime.now(ET)


def owned_positions(client: IBKRClient, journal: Journal) -> dict:
    owned = set().union(*(journal.owned_stocks(s) for s in STRATEGIES))
    return {
        p.contract.symbol: p for p in client.positions()
        if p.position > 0 and p.contract.secType == "STK" and p.contract.symbol in owned
    }


def main() -> None:
    if not settings.step_stop_enabled:
        logger.info("STEP_STOP_ENABLED=false — nothing to do")
        return
    journal = Journal(settings.db_path)

    with IBKRClient(settings) as client:
        ib = client.ib
        ib.reqMarketDataType(3)  # live where subscribed, delayed otherwise

        now = _now_et()
        if now.time() < MARKET_OPEN:
            wait_s = (datetime.combine(now.date(), MARKET_OPEN, tzinfo=ET) - now).total_seconds()
            logger.info("Waiting %.0fs for 9:30am ET", wait_s)
            ib.sleep(max(0.0, wait_s))

        positions = owned_positions(client, journal)
        ensure_protective_stops(ib, journal, positions, settings.stop_loss_pct, SOURCE)
        logger.info("Step-stop manager starting for %s", sorted(positions))

        tickers = {}
        warned_no_stop: set[str] = set()
        flat_last_tick: set[str] = set()
        while _now_et().time() < MARKET_CLOSE:
            positions = owned_positions(client, journal)
            held = {p.contract.symbol for p in client.positions() if p.position > 0 and p.contract.secType == "STK"}
            # A stop only counts as orphaned once its symbol has looked flat
            # on two consecutive ticks: right at the open, a bracket BUY can
            # have filled a moment before IBKR reports the new position.
            stop_symbols = {t.contract.symbol for t in ib.reqOpenOrders() if t.order.orderType == "STP"}
            flat_now = stop_symbols - held
            cancel_orphan_stops(ib, held | (flat_now - flat_last_tick))
            flat_last_tick = flat_now
            open_trades = ib.reqOpenOrders()
            for symbol, pos in positions.items():
                if symbol not in tickers:
                    contract = Stock(symbol, "SMART", "USD")
                    ib.qualifyContracts(contract)
                    tickers[symbol] = ib.reqMktData(contract)
                    ib.sleep(2)
                price = tickers[symbol].marketPrice()
                if price is None or math.isnan(price) or price <= 0:
                    continue

                stops = [
                    t for t in open_trades
                    if t.contract.symbol == symbol and t.order.action == "SELL" and t.order.orderType == "STP"
                ]
                if not stops:
                    if symbol not in warned_no_stop:
                        logger.warning("%s: no working stop to raise (the 4:30pm audit will re-place it)", symbol)
                        warned_no_stop.add(symbol)
                    continue

                current = max(t.order.auxPrice for t in stops)
                new_stop = plan_stop_raise(
                    pos.avgCost, price, current, settings.step_stop_pct, settings.step_stop_buffer_pct,
                    max_loss_pct=settings.stop_loss_pct,
                )
                if new_stop is None:
                    continue
                for t in stops:
                    t.order.auxPrice = new_stop
                    ib.placeOrder(t.contract, t.order)
                gain = (price / pos.avgCost - 1) * 100
                logger.info(
                    "%s: %+.1f%% (price %.2f, cost %.2f) — stop raised %.2f -> %.2f",
                    symbol, gain, price, pos.avgCost, current, new_stop,
                )
                journal.log_order(symbol, "SELL", int(pos.position), "STP_RAISE", broker="IBKR",
                                  status="modified", stop_price=new_stop)

            ib.sleep(TICK_SECONDS)

        for ticker in tickers.values():
            ib.cancelMktData(ticker.contract)
        logger.info("Step-stop manager finished for today")


if __name__ == "__main__":
    main()
