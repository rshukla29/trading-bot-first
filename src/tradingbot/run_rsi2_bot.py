"""Daily entrypoint for the RSI(2) mean-reversion strategy: connect to
IBKR, evaluate rsi2_symbols, manage existing positions against the RSI(2)
overbought exit, open new positions sized by risk-per-trade with an
attached stop-loss, and log everything.

Runs against `settings.rsi2_symbols` — a watchlist deliberately kept
separate from `settings.symbols` (the MA-crossover bot's watchlist) so the
two independent stock strategies never end up fighting over the same
position (see config.py).

Intended to be run once per day (after market data for the day is final)
via a scheduled task. Refuses to trade on a non-paper account unless
LIVE_TRADING=true is explicitly set (see ibkr/client.py).

    python -m tradingbot.run_rsi2_bot
"""
from __future__ import annotations

import logging

from tradingbot.config import settings
from tradingbot.ibkr.client import IBKRClient
from tradingbot.ibkr.data import fetch_daily_bars
from tradingbot.ibkr.orders import place_bracket_buy, place_market_sell
from tradingbot.journal import Journal
from tradingbot.risk import DailyLossKillSwitch, can_open_new_position, position_size
from tradingbot.strategy.rsi2_mean_reversion import RSI2MeanReversionStrategy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    strategy = RSI2MeanReversionStrategy(
        trend_ma=settings.rsi2_trend_ma,
        rsi_period=settings.rsi2_period,
        buy_threshold=settings.rsi2_buy_threshold,
        sell_threshold=settings.rsi2_sell_threshold,
        fib_lookback=settings.rsi2_fib_lookback or None,
        fib_zone=(settings.rsi2_fib_zone_low, settings.rsi2_fib_zone_high),
    )
    journal = Journal(settings.db_path)

    with IBKRClient(settings) as client:
        equity = client.net_liquidation()
        kill_switch = DailyLossKillSwitch(settings.max_daily_loss_pct, equity)

        open_positions = {p.contract.symbol: p for p in client.positions() if p.position != 0}
        rsi2_positions = {s: p for s, p in open_positions.items() if s in settings.rsi2_symbols}
        logger.info("Equity: %.2f | RSI2 open positions: %s", equity, list(rsi2_positions))

        for symbol in settings.rsi2_symbols:
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
            journal.log_signal(signal.symbol, signal.action, signal.price, signal.reason, source="run_rsi2_bot")

            held = symbol in rsi2_positions

            if held and signal.action == "EXIT":
                qty = int(rsi2_positions[symbol].position)
                place_market_sell(client.ib, symbol, qty)
                journal.log_order(symbol, "SELL", qty, "MKT", broker="IBKR", status="submitted")
                logger.info("EXIT %s x%d (%s)", symbol, qty, signal.reason)
                continue

            if held:
                continue  # already in a position, no RSI overbought exit today; stop-loss is broker-side

            if signal.action != "BUY":
                continue

            if kill_switch.is_tripped():
                logger.warning("Daily loss kill-switch tripped, skipping new entries for %s", symbol)
                continue

            if not can_open_new_position(len(rsi2_positions), settings.max_positions):
                logger.info("Max positions (%d) reached, skipping %s", settings.max_positions, symbol)
                continue

            qty = position_size(equity, signal.price, settings.risk_pct_per_trade, settings.stop_loss_pct)
            if qty <= 0:
                logger.info("Computed position size 0 for %s, skipping", symbol)
                continue

            place_bracket_buy(client.ib, symbol, qty, signal.price, settings.stop_loss_pct)
            journal.log_order(
                symbol, "BUY", qty, "MKT+STP", broker="IBKR", status="submitted",
                stop_price=round(signal.price * (1 - settings.stop_loss_pct), 2),
            )
            rsi2_positions[symbol] = None  # mark as filled-in-this-run to respect max_positions
            logger.info("BUY %s x%d @ ~%.2f (%s)", symbol, qty, signal.price, signal.reason)


if __name__ == "__main__":
    main()
