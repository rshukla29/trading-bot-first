"""Long-running intraday entrypoint for the Opening Range Breakout options
strategy: connects once shortly before market open, watches for an
opening-range breakout on each newly-completed 5-min bar, and — once a
position is open — monitors it for its exit conditions every ~60s using
real live option quotes (a strict improvement over the backtest, which
had to simulate premiums via Black-Scholes since no real historical
options data was available on this account). Runs until end of day, then
exits the process; a scheduled task starts it fresh each morning rather
than re-invoking it repeatedly.

Trades each symbol in settings.orb_symbol_list independently (one
position per symbol; different symbols can be open at once). Per symbol,
single open position at a time —
mirrors options/backtest.py's own single open_trade slot exactly, so live
behavior matches what was actually backtested. 0DTE by default
(options_dte_days=0): this strategy is meaningfully higher-risk than the
daily-bar bots (low win rate, large average win per the ORB strategy's own
docstring) — settings.options_max_contracts_per_trade caps sizing
regardless of what the risk-pct formula computes.

Exits (since 2026-09-28): a completed 5-min bar closing back across the
session VWAP, or the 15:55 ET flatten. The older rules (-30% premium
stop, +50% target, range re-entry, 10% step stop) are still available via
settings but off — scripts/orb_exit_comparison.py found each of them
removed the edge (see config.py's options_vwap_exit comment). Priority
when several are enabled: eod_flatten > stop_loss > step_stop >
profit_target > vwap_cross > range_reentry, identical to the backtest —
see options/live_orb.py:determine_exit_reason.

If the process crashes and is restarted mid-day, it recovers any
already-open position from the journal (orb_positions table) rather than
losing track of it — see recover_open_position.

Refuses to trade on a non-paper account unless LIVE_TRADING=true.
Intended to be started once daily via a scheduled task, shortly before
9:30am ET.

    python -m tradingbot.run_orb_bot
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from ib_async import Option, Stock

from tradingbot.config import settings
from tradingbot.ibkr.client import IBKRClient
from tradingbot.ibkr.data import fetch_intraday_bars
from tradingbot.indicators import session_vwap
from tradingbot.journal import Journal
from tradingbot.options.live_chain import get_quote, get_real_chain
from tradingbot.options.live_orb import (
    CONTRACT_MULTIPLIER,
    determine_exit_reason,
    enter_orb_trade,
    exit_orb_trade,
    select_orb_contract,
    wait_for_fill,
)
from tradingbot.options.orb_strategy import OpeningRangeBreakoutStrategy
from tradingbot.risk import step_trailing_stop
from tradingbot.strategy.trend_join import completed_bars

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("ib_async").setLevel(logging.WARNING)  # its own wrapper logs every tick/portfolio update at INFO
logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
TICK_SECONDS = 60.0


def _now_et() -> datetime:
    return datetime.now(ET)


def _rehydrate_option(ib, conid: int) -> Option:
    contract = Option(conId=conid, exchange="SMART")
    qualified = ib.qualifyContracts(contract)
    if not qualified or qualified[0] is None:
        raise ValueError(f"Could not rehydrate ORB option contract conId={conid}")
    return qualified[0]


def recover_open_position(ib, journal: Journal, symbol: str) -> dict | None:
    row = journal.open_orb_position_row(symbol)
    if row is None:
        return None
    contract = _rehydrate_option(ib, row["conid"])
    logger.warning(
        "Recovered open ORB position from journal: %s %s%s x%d (entry_premium=%.2f) — resuming monitoring",
        symbol, row["strike"], row["right"], row["contracts"], row["entry_premium"],
    )
    return {
        "id": row["id"], "contract": contract, "right": row["right"],
        "entry_premium": row["entry_premium"], "contracts": row["contracts"],
        "range_low": row["range_low"], "range_high": row["range_high"],
    }


def latest_bar_vs_vwap(ib, symbol: str) -> tuple[float | None, float | None]:
    """(close, session VWAP) of today's last *completed* 5-min bar. IBKR
    returns the still-forming bar last, which must not count — the rule is
    a 5-min close across VWAP, not a tick through it."""
    bm = settings.options_bar_minutes
    now = pd.Timestamp(_now_et())
    bars = fetch_intraday_bars(ib, symbol, bm, duration="1 D")
    bars = completed_bars(bars[bars.index.date == now.date()], now, bm)
    if bars.empty:
        return None, None
    return float(bars["close"].iloc[-1]), float(session_vwap(bars).iloc[-1])


def check_exit(ib, journal: Journal, symbol: str, underlying: Stock, position: dict) -> bool:
    """Returns True if the position was closed this call."""
    _, _, spot_mid = get_quote(ib, underlying)
    _, _, premium_mid = get_quote(ib, position["contract"])

    # Step stop is based on the highest premium seen, so a spike to +21%
    # that fades still exits at +20%. Peak is in-memory only: after a
    # restart it rebuilds from the current premium.
    position["peak_premium"] = max(position.get("peak_premium", position["entry_premium"]), premium_mid)
    step_stop = None
    if settings.options_step_stop_enabled:
        step_stop = step_trailing_stop(
            position["entry_premium"], position["peak_premium"], settings.step_stop_pct, settings.step_stop_buffer_pct,
        )

    bar_close = vwap = None
    if settings.options_vwap_exit:
        bar_close, vwap = latest_bar_vs_vwap(ib, symbol)

    reason = determine_exit_reason(
        now_time=_now_et().time(),
        current_premium=premium_mid,
        entry_premium=position["entry_premium"],
        stop_loss_pct_of_premium=settings.options_stop_loss_pct_of_premium,
        profit_target_pct=settings.options_profit_target_pct or None,
        spot=spot_mid,
        right=position["right"],
        range_low=position["range_low"],
        range_high=position["range_high"],
        exit_on_range_reentry=settings.options_exit_on_range_reentry,
        step_stop_premium=step_stop,
        use_stop_loss=settings.options_use_stop_loss,
        bar_close=bar_close,
        vwap=vwap,
    )
    if reason is None:
        logger.info(
            "%s: ORB position holding (premium %.2f vs entry %.2f, spot %.2f, last 5-min close %s vs VWAP %s)",
            symbol, premium_mid, position["entry_premium"], spot_mid,
            f"{bar_close:.2f}" if bar_close is not None else "n/a", f"{vwap:.2f}" if vwap is not None else "n/a",
        )
        return False

    trade, _ = exit_orb_trade(ib, position["contract"], position["contracts"])
    filled = wait_for_fill(ib, trade)
    if not filled:
        logger.warning(
            "%s: ORB close order did not fill (status=%s) — will retry next tick",
            symbol, trade.orderStatus.status,
        )
        ib.cancelOrder(trade.order)
        return False

    actual_exit = trade.orderStatus.avgFillPrice
    realized_pnl = (actual_exit - position["entry_premium"]) * position["contracts"] * CONTRACT_MULTIPLIER
    journal.close_orb_position(
        position["id"], datetime.now(timezone.utc).isoformat(), actual_exit, reason, realized_pnl,
    )
    journal.log_order(symbol, "CLOSE_ORB", position["contracts"], "OPT", broker="IBKR", status="filled")
    logger.info(
        "%s: CLOSED ORB position (%s), actual exit premium=%.2f, pnl=%.2f",
        symbol, reason, actual_exit, realized_pnl,
    )
    return True


def check_entry(
    ib, journal: Journal, symbol: str, strategy: OpeningRangeBreakoutStrategy, chain, equity: float,
) -> dict | None:
    bars = fetch_intraday_bars(ib, symbol, settings.options_bar_minutes)
    signal = strategy.generate_signal(symbol, bars)
    journal.log_signal(signal.symbol, signal.action, signal.underlying_price, signal.reason, source="run_orb_bot")

    if signal.action not in ("BUY_CALL", "BUY_PUT"):
        logger.info("%s: %s (%s)", symbol, signal.action, signal.reason)
        return None

    right = "C" if signal.action == "BUY_CALL" else "P"
    day_range = strategy.range_for_day(bars, bars.index[-1].date())
    if day_range is None:
        logger.warning("%s: breakout signal fired but opening range unavailable, skipping", symbol)
        return None
    range_low, range_high = day_range

    contract = select_orb_contract(
        ib, symbol, chain, signal.underlying_price, right,
        settings.options_otm_pct, settings.options_strike_increment, settings.options_dte_days,
    )
    _, _, mid = get_quote(ib, contract)
    if mid <= 0.01:
        logger.warning("%s: quoted premium %.3f too small, skipping", symbol, mid)
        return None

    contracts = int((equity * settings.options_risk_pct_per_trade) // (mid * CONTRACT_MULTIPLIER))
    if contracts > settings.options_max_contracts_per_trade:
        logger.info("%s: risk-sized %d contracts, capping at %d", symbol, contracts, settings.options_max_contracts_per_trade)
        contracts = settings.options_max_contracts_per_trade
    if contracts <= 0:
        logger.info("%s: computed 0 contracts, skipping", symbol)
        return None

    trade, _ = enter_orb_trade(ib, contract, contracts)
    filled = wait_for_fill(ib, trade)
    if not filled:
        logger.warning("%s: ORB open order did not fill (status=%s) — cancelling", symbol, trade.orderStatus.status)
        ib.cancelOrder(trade.order)
        return None

    actual_entry = trade.orderStatus.avgFillPrice
    position_id = journal.open_orb_position(
        symbol, datetime.now(timezone.utc).isoformat(), contract.lastTradeDateOrContractMonth,
        contract.strike, right, contract.conId, actual_entry, contracts, range_low, range_high,
    )
    journal.log_order(symbol, "OPEN_ORB", contracts, "OPT", broker="IBKR", status="filled")
    logger.info(
        "%s: OPENED ORB %s %s%s x%d, actual entry premium=%.2f (%s)",
        symbol, contract.lastTradeDateOrContractMonth, contract.strike, right, contracts, actual_entry, signal.reason,
    )
    return {
        "id": position_id, "contract": contract, "right": right, "entry_premium": actual_entry,
        "contracts": contracts, "range_low": range_low, "range_high": range_high,
    }


def tick_symbol(ib, client, journal: Journal, strategy: OpeningRangeBreakoutStrategy, state: dict) -> None:
    """One monitoring tick for one symbol: manage its open position, or
    look for an entry on each newly-completed bar."""
    symbol = state["symbol"]
    if state["position"] is not None:
        if check_exit(ib, journal, symbol, state["underlying"], state["position"]):
            state["position"] = None
        return
    bars = fetch_intraday_bars(ib, symbol, settings.options_bar_minutes)
    latest_bar_ts = bars.index[-1]
    if latest_bar_ts != state["last_bar_ts"]:
        state["last_bar_ts"] = latest_bar_ts
        state["position"] = check_entry(ib, journal, symbol, strategy, state["chain"], client.net_liquidation())


def main() -> None:
    symbols = settings.orb_symbol_list
    strategy = OpeningRangeBreakoutStrategy(
        opening_range_minutes=settings.options_opening_range_minutes, bar_minutes=settings.options_bar_minutes,
    )
    journal = Journal(settings.db_path)

    with IBKRClient(settings, client_id=settings.orb_client_id) as client:
        ib = client.ib
        states = []
        for symbol in symbols:
            # A symbol that can't be set up (no chain, no data) is skipped
            # for the day rather than stopping the others.
            try:
                underlying = Stock(symbol, "SMART", "USD")
                ib.qualifyContracts(underlying)
                states.append({
                    "symbol": symbol, "underlying": underlying, "chain": get_real_chain(ib, symbol),
                    "position": recover_open_position(ib, journal, symbol), "last_bar_ts": None,
                })
            except Exception:
                logger.exception("%s: ORB setup failed — skipping this symbol today", symbol)

        now = _now_et()
        if now.time() < MARKET_OPEN:
            wait_s = (datetime.combine(now.date(), MARKET_OPEN, tzinfo=ET) - now).total_seconds()
            logger.info("Started before market open, waiting %.0fs for 9:30am ET", wait_s)
            ib.sleep(max(0.0, wait_s))

        logger.info("ORB live loop starting for %s", [s["symbol"] for s in states])
        while _now_et().time() < MARKET_CLOSE:
            for state in states:
                try:
                    tick_symbol(ib, client, journal, strategy, state)
                except Exception:
                    # One symbol's failure (bad quote, data error) must not
                    # stop monitoring the others' open positions.
                    logger.exception("%s: ORB tick failed — will retry next tick", state["symbol"])
            ib.sleep(TICK_SECONDS)

        for state in states:
            if state["position"] is not None:
                logger.warning("%s: still open at market close, final safety-net flatten", state["symbol"])
                try:
                    check_exit(ib, journal, state["symbol"], state["underlying"], state["position"])
                except Exception:
                    logger.exception("%s: final flatten failed", state["symbol"])

        logger.info("ORB live loop finished for today")


if __name__ == "__main__":
    main()
