"""Daily entrypoint for the RSI(2) mean-reversion strategy: connect to
IBKR, evaluate the entry universe, manage existing positions against the
RSI(2) overbought exit, open new positions sized by risk-per-trade with an
attached stop-loss, and log everything.

The entry universe is `settings.rsi2_symbols` (the fixed watchlist) plus,
when `rsi2_scan_top_movers` is enabled, today's top US gainers/losers/
most-active from IBKR's scanner (see ibkr/scanner.py) — the fixed list
alone can miss a real setup on a symbol that just isn't one of those 10
names today. Movers overlapping `settings.symbols` (the MA-crossover
bot's watchlist) are excluded so the two independent stock strategies
never end up fighting over the same position (see config.py). Any
currently-held qualifying stock position is always evaluated for its EXIT
signal even if its symbol drops off a later day's scan.

Two guardrails specific to scanner-discovered movers, added after a real
incident on 2026-09-11 (three same-day BUY orders — CADL, OMDA, PMTS —
all still pending, unfilled, when the weekend arrived, each a scanner
pick with elevated single-day volume):

  - The movers scan is skipped entirely on Fridays (see
    should_scan_movers_today): a Friday entry would sit queued over the
    full weekend before it could even fill, compounding weekend gap risk
    on top of a name the scanner already selected *for* being unusually
    volatile that specific day. The fixed watchlist still trades normally
    on Fridays — only new scanner-sourced entries are skipped, and
    existing positions (from any source) are still managed/exited daily
    regardless of day of week.
  - A volume-spike filter blocks any BUY (from either source) when
    today's volume is an extreme multiple of normal — the signature of a
    news/event-driven move rather than routine technical price action,
    which the RSI(2) edge doesn't reliably transfer to. This is a partial
    mitigation, not a complete fix: checked against the 2026-09-11
    incident, it would have caught PMTS (21.5x normal volume) and CADL
    (3.36x), but not OMDA (only 1.75x — not a volume-spike signature at
    all despite also being an ungrounded, now-stale signal by Monday).

Intended to be run once per day (after market data for the day is final)
via a scheduled task. Refuses to trade on a non-paper account unless
LIVE_TRADING=true is explicitly set (see ibkr/client.py).

    python -m tradingbot.run_rsi2_bot
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from tradingbot.config import settings
from tradingbot.ibkr.client import IBKRClient
from tradingbot.ibkr.data import fetch_daily_bars
from tradingbot.ibkr.orders import place_bracket_buy, place_market_sell
from tradingbot.ibkr.scanner import fetch_top_movers, merge_scan_universe, should_scan_movers_today
from tradingbot.indicators import volume_spike_ratio
from tradingbot.journal import Journal
from tradingbot.risk import DailyLossKillSwitch, can_open_new_position, position_size
from tradingbot.strategy.rsi2_mean_reversion import RSI2MeanReversionStrategy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


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

        # Any held stock (non-option) position outside the MA-crossover
        # watchlist is unambiguously ours: MA-crossover only ever trades
        # settings.symbols, and RSI2 is the only other strategy that
        # trades equities. This lets a position opened via a dynamic
        # mover still get evaluated for its EXIT signal on a later day
        # even if that symbol has since dropped off the scanner.
        rsi2_positions = {
            p.contract.symbol: p for p in client.positions()
            if p.position != 0 and p.contract.secType == "STK" and p.contract.symbol not in settings.symbols
        }
        logger.info("Equity: %.2f | RSI2 open positions: %s", equity, list(rsi2_positions))

        movers: list[str] = []
        if settings.rsi2_scan_top_movers and not should_scan_movers_today(datetime.now(ET)):
            logger.info("Friday: skipping top-movers scan for new entries (weekend gap risk) — fixed watchlist only")
        elif settings.rsi2_scan_top_movers:
            try:
                movers = fetch_top_movers(
                    client.ib, settings.rsi2_scan_rows, settings.rsi2_scan_min_price, settings.rsi2_scan_min_volume,
                )
                logger.info("Top movers scan found %d candidates: %s", len(movers), movers)
            except Exception:
                logger.exception("Top-movers scan failed, continuing with fixed watchlist only")

        entry_universe = merge_scan_universe(settings.rsi2_symbols, movers, settings.symbols)
        symbols_to_check = sorted(set(entry_universe) | set(rsi2_positions))

        for symbol in symbols_to_check:
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

            spike_ratio = volume_spike_ratio(bars["volume"], settings.rsi2_volume_spike_window).iloc[-1]
            if pd.notna(spike_ratio) and spike_ratio > settings.rsi2_volume_spike_multiplier:
                logger.info(
                    "%s: BUY blocked, volume %.1fx normal (>%0.1fx threshold) — likely news-driven, not a technical dip",
                    symbol, spike_ratio, settings.rsi2_volume_spike_multiplier,
                )
                continue

            if kill_switch.is_tripped():
                logger.warning("Daily loss kill-switch tripped, skipping new entries for %s", symbol)
                continue

            if not can_open_new_position(len(rsi2_positions), settings.max_positions):
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
            journal.log_order(
                symbol, "BUY", qty, "MKT+STP", broker="IBKR", status="submitted",
                stop_price=round(signal.price * (1 - settings.stop_loss_pct), 2),
            )
            rsi2_positions[symbol] = None  # mark as filled-in-this-run to respect max_positions
            logger.info("BUY %s x%d @ ~%.2f (%s)", symbol, qty, signal.price, signal.reason)


if __name__ == "__main__":
    main()
