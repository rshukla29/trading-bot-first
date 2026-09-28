"""Daily entrypoint: connect to IBKR, evaluate the watchlist, manage
existing positions against the strategy's EXIT signal, open new positions
sized by risk-per-trade with an attached stop-loss, and log everything.

Intended to be run once per day (after market data for the day is final)
via a scheduled task. Refuses to trade on a non-paper account unless
LIVE_TRADING=true is explicitly set (see ibkr/client.py).

    python -m tradingbot.run_ibkr_bot
"""
from __future__ import annotations

import logging

from tradingbot.config import settings
from tradingbot.ibkr.client import IBKRClient
from tradingbot.ibkr.data import fetch_daily_bars
from tradingbot.ibkr.orders import place_bracket_buy, place_market_sell
from tradingbot.journal import Journal
from tradingbot.ownership import resolve_ownership
from tradingbot.protection import cancel_orphan_stops, ensure_protective_stops
from tradingbot.risk import DailyLossKillSwitch, can_open_new_position, position_size
from tradingbot.strategy.factory import build_strategy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

STRATEGY = "ma_crossover"


def main() -> None:
    strategy = build_strategy(settings)
    journal = Journal(settings.db_path)

    with IBKRClient(settings) as client:
        equity = client.net_liquidation()
        kill_switch = DailyLossKillSwitch(settings.max_daily_loss_pct, equity)

        # Only positions this bot opened itself (see ownership.py) — never a
        # manual trade or another strategy's position, even on the watchlist.
        held = {
            p.contract.symbol: p for p in client.positions()
            if p.position != 0 and p.contract.secType == "STK"
        }
        open_order_symbols = {t.contract.symbol for t in client.ib.reqAllOpenOrders()}
        ownership = resolve_ownership(journal, STRATEGY, set(held), open_order_symbols)
        open_positions = {s: held[s] for s in ownership.mine}
        # Every owned position must have a working stop; anything already
        # through its 10% level without one is sold (see protection.py).
        cancel_orphan_stops(client.ib, {s for s, p in held.items() if p.position > 0})
        stop_audit_sold = ensure_protective_stops(client.ib, journal, open_positions, settings.stop_loss_pct, "run_ibkr_bot")
        for symbol in stop_audit_sold:
            open_positions.pop(symbol)
        # Not-mine positions are never touched; audit-sold ones aren't re-bought this run.
        unmanaged = (ownership.not_mine & set(settings.symbols)) | stop_audit_sold
        logger.info("Equity: %.2f | MA positions: %s", equity, list(open_positions))
        if unmanaged:
            logger.info("Held but not opened by this bot (left alone): %s", sorted(unmanaged))

        for symbol in settings.symbols:
            if symbol in unmanaged:
                continue
            try:
                bars = fetch_daily_bars(client.ib, symbol, duration="2 Y")
            except Exception:
                logger.exception("Failed to fetch bars for %s, skipping", symbol)
                continue

            if len(bars) < strategy.min_history:
                logger.warning(
                    "%s: only %d bars, strategy needs %d — skipping",
                    symbol, len(bars), strategy.min_history,
                )
                continue

            signal = strategy.generate_signal(symbol, bars)
            journal.log_signal(signal.symbol, signal.action, signal.price, signal.reason, source="run_ibkr_bot")

            held = symbol in open_positions

            if held and signal.action == "EXIT":
                qty = int(open_positions[symbol].position)
                trade = place_market_sell(client.ib, symbol, qty)
                journal.log_order(symbol, "SELL", qty, "MKT", broker="IBKR", status="submitted")
                logger.info("EXIT %s x%d (%s)", symbol, qty, signal.reason)
                continue

            if held:
                continue  # already in a position, no crossover exit signal today; stop-loss is broker-side

            if signal.action != "BUY":
                continue

            if kill_switch.is_tripped():
                logger.warning("Daily loss kill-switch tripped, skipping new entries for %s", symbol)
                continue

            if not can_open_new_position(len(open_positions), settings.max_positions):
                logger.info("Max positions (%d) reached, skipping %s", settings.max_positions, symbol)
                continue

            qty = position_size(
                equity, signal.price, settings.risk_pct_per_trade, settings.stop_loss_pct,
                settings.max_position_value,
            )
            if qty <= 0:
                logger.info("Computed position size 0 for %s, skipping", symbol)
                continue

            place_bracket_buy(client.ib, symbol, qty, signal.price, settings.stop_loss_pct)
            journal.claim_stock(symbol, STRATEGY)
            journal.log_order(
                symbol, "BUY", qty, "MKT+STP", broker="IBKR", status="submitted",
                stop_price=round(signal.price * (1 - settings.stop_loss_pct), 2),
            )
            open_positions[symbol] = None  # mark as filled-in-this-run to respect max_positions
            logger.info("BUY %s x%d @ ~%.2f (%s)", symbol, qty, signal.price, signal.reason)


if __name__ == "__main__":
    main()
