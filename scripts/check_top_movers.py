"""Ad-hoc, read-only check: pull today's top US stock movers from IBKR's
market scanner and run both live stock strategies (MA-crossover,
RSI(2) mean-reversion) against them to see if any would generate a signal
right now. Does NOT place orders and is independent of the fixed
watchlists the scheduled bots trade — purely exploratory.

    python -m scripts.check_top_movers
"""
from __future__ import annotations

import logging

from ib_async import IB

from tradingbot.config import settings
from tradingbot.ibkr.data import fetch_daily_bars
from tradingbot.ibkr.scanner import fetch_top_movers
from tradingbot.strategy.factory import build_strategy
from tradingbot.strategy.rsi2_mean_reversion import RSI2MeanReversionStrategy

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    ib = IB()
    ib.connect(settings.ibkr_host, settings.ibkr_port, clientId=settings.ibkr_client_id + 50)

    try:
        all_symbols = fetch_top_movers(
            ib, settings.rsi2_scan_rows, settings.rsi2_scan_min_price, settings.rsi2_scan_min_volume,
        )

        print(f"\n=== Today's top US movers (IBKR scanner, {len(all_symbols)} unique) ===")
        print(", ".join(all_symbols))

        ma_strategy = build_strategy(settings)
        rsi2_strategy = RSI2MeanReversionStrategy(
            trend_ma=settings.rsi2_trend_ma,
            rsi_period=settings.rsi2_period,
            buy_threshold=settings.rsi2_buy_threshold,
            sell_threshold=settings.rsi2_sell_threshold,
            fib_lookback=settings.rsi2_fib_lookback or None,
            fib_zone=(settings.rsi2_fib_zone_low, settings.rsi2_fib_zone_high),
        )

        print(f"\n=== Checking {len(all_symbols)} unique movers against both strategies ===")
        any_hit = False
        for symbol in all_symbols:
            try:
                bars = fetch_daily_bars(ib, symbol, duration="2 Y")
            except Exception as e:
                print(f"{symbol}: skipped, no data ({e})")
                continue

            if len(bars) < max(ma_strategy.min_history, rsi2_strategy.min_history):
                print(f"{symbol}: skipped, insufficient history ({len(bars)} bars)")
                continue

            ma_signal = ma_strategy.generate_signal(symbol, bars)
            rsi2_signal = rsi2_strategy.generate_signal(symbol, bars)

            hits = []
            if ma_signal.action == "BUY":
                hits.append(f"MA-crossover BUY ({ma_signal.reason})")
            if rsi2_signal.action == "BUY":
                hits.append(f"RSI(2) BUY ({rsi2_signal.reason})")

            if hits:
                any_hit = True
                print(f"{symbol}: {' | '.join(hits)}")

        if not any_hit:
            print("No BUY signals from either strategy among today's top movers.")

    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
