"""Daily entrypoint for the live iron condor (credit spread) strategy:
manage existing positions against profit-target / stop-loss /
21-DTE-forced-close, open new laddered positions on schedule, using real
IBKR option chains and real live bid/ask for order pricing.

Symbols come from settings.credit_spread_symbol_list — SPY-only for now.
QQQ and IWM options both failed to fill across every live test (QQQ threw
explicit "additional subscription required" errors; IWM failed silently)
while SPY filled cleanly — an account-level options data entitlement gap,
not a code bug. Add them back to CREDIT_SPREAD_SYMBOLS once resolved; the
backtest already covers all three.

Strike *selection* still uses a Black-Scholes delta estimate off realized
volatility (same method validated in backtesting) rather than IBKR's live
modelGreeks, which weren't reliably populating as of this build — only
approximate targeting is needed there, since actual order pricing uses
real quotes. Position tracking is our own bookkeeping (journal's
condor_positions table): IBKR's raw position list has no notion of "these
4 legs are one spread."

IBKR combo-order convention (see options/live_condor.py): the parent
Order.action is always "BUY" — each ComboLeg carries its own true
BUY/SELL action, and the parent's lmtPrice sign carries the credit/debit:
negative = net credit received, positive = net debit paid. Fills on paper
combo orders were observed taking up to ~60s even at a marketable price —
much slower than single-leg orders — so every order placement here waits
(wait_for_fill) rather than checking once and giving up.

Intended to run once per day, after market close, via a scheduled task —
but NOT scheduled automatically until verified with at least one manual
run against the paper account, given this is new multi-leg order-placement
code. Refuses to trade on a non-paper account unless LIVE_TRADING=true.

    python -m tradingbot.run_iron_condor_bot
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from ib_async import Contract

from tradingbot.config import settings
from tradingbot.ibkr.client import IBKRClient
from tradingbot.ibkr.data import fetch_daily_bars
from tradingbot.journal import Journal
from tradingbot.options.iron_condor import CONTRACT_MULTIPLIER, TRADING_DAYS_PER_YEAR
from tradingbot.options.live_chain import get_real_chain
from tradingbot.options.live_condor import (
    CondorLegs,
    close_condor,
    condor_close_ladder,
    condor_open_ladder,
    net_credit_estimate,
    open_condor,
    select_condor_legs,
    wait_for_fill,
)
from tradingbot.options.pricing import realized_volatility

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("ib_async").setLevel(logging.WARNING)  # its own wrapper logs every tick/portfolio update at INFO
logger = logging.getLogger(__name__)


def _rehydrate_option(ib, conid: int):
    contract = Contract(conId=conid, exchange="SMART")
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        raise ValueError(f"Could not rehydrate option contract conId={conid}")
    return qualified[0]


def manage_open_positions(ib, journal: Journal, symbol: str) -> None:
    for pos in journal.open_condor_positions(symbol):
        legs = CondorLegs(
            call_short=_rehydrate_option(ib, pos["call_short_conid"]),
            call_long=_rehydrate_option(ib, pos["call_long_conid"]),
            put_short=_rehydrate_option(ib, pos["put_short_conid"]),
            put_long=_rehydrate_option(ib, pos["put_long_conid"]),
            expiry=pos["expiry"],
        )
        cost = net_credit_estimate(ib, legs)
        credit = pos["credit_received"]
        expiry_date = datetime.strptime(pos["expiry"], "%Y%m%d").date()
        days_to_expiry = (expiry_date - date.today()).days

        exit_reason = None
        if cost <= credit * (1 - settings.credit_spread_profit_target_pct):
            exit_reason = "profit_target"
        elif cost >= credit * settings.credit_spread_stop_loss_multiple:
            exit_reason = "stop_loss"
        elif days_to_expiry <= settings.credit_spread_dte_forced_close:
            exit_reason = "dte_forced_close"

        if exit_reason is None:
            logger.info("%s: position %d holding (cost=%.2f credit=%.2f dte=%d)",
                        symbol, pos["id"], cost, credit, days_to_expiry)
            continue

        # Step from the mid toward the natural price, re-quoting each attempt
        # (mid-only pricing left a condor unclosed for days).
        remaining, cost_sum = pos["contracts"], 0.0
        for attempt in range(3):
            ladder = condor_close_ladder(ib, legs)
            limit = ladder[min(attempt, len(ladder) - 1)]
            trade, _ = close_condor(ib, symbol, legs, remaining, limit)
            if not wait_for_fill(ib, trade, timeout=60):
                ib.cancelOrder(trade.order)
                ib.sleep(2)
            got = int(trade.orderStatus.filled)
            if got:
                cost_sum += abs(trade.orderStatus.avgFillPrice) * got
                remaining -= got
            if remaining == 0:
                break
        if remaining:
            logger.warning("%s: close for position %d: %d of %d contracts still open after 3 attempts "
                           "(%s) — will retry next run", symbol, pos["id"], remaining, pos["contracts"], exit_reason)
            continue

        actual_cost = cost_sum / pos["contracts"]
        realized_pnl = (credit - actual_cost) * pos["contracts"] * CONTRACT_MULTIPLIER
        journal.close_condor_position(
            pos["id"], datetime.now(timezone.utc).isoformat(), actual_cost, exit_reason, realized_pnl,
        )
        journal.log_order(symbol, "CLOSE_CONDOR", pos["contracts"], "COMBO", broker="IBKR", status="filled")
        logger.info("%s: CLOSED position %d (%s), actual cost=%.2f, pnl=%.2f",
                    symbol, pos["id"], exit_reason, actual_cost, realized_pnl)


def maybe_open_new_position(ib, journal: Journal, symbol: str, equity: float) -> None:
    open_positions = journal.open_condor_positions(symbol)
    if len(open_positions) >= settings.credit_spread_max_concurrent:
        logger.info("%s: max concurrent (%d) reached, skipping entry", symbol, settings.credit_spread_max_concurrent)
        return

    last_entry = journal.last_condor_entry_date(symbol)
    if last_entry is not None:
        days_since = (date.today() - datetime.fromisoformat(last_entry).date()).days
        if days_since < settings.credit_spread_entry_interval_days:
            logger.info("%s: only %d days since last entry (need %d), skipping",
                        symbol, days_since, settings.credit_spread_entry_interval_days)
            return

    bars = fetch_daily_bars(ib, symbol, duration="1 Y")
    spot = float(bars["close"].iloc[-1])
    vol_series = realized_volatility(bars["close"], settings.credit_spread_vol_window, TRADING_DAYS_PER_YEAR)
    vol = float(vol_series.iloc[-1]) * settings.credit_spread_iv_premium_multiplier

    chain = get_real_chain(ib, symbol)
    legs = select_condor_legs(
        ib, symbol, chain, spot, vol, settings.credit_spread_rate,
        settings.credit_spread_target_delta, settings.credit_spread_wing_width, settings.credit_spread_dte_days,
    )

    credit_estimate = net_credit_estimate(ib, legs)
    max_loss_per_contract = settings.credit_spread_wing_width * CONTRACT_MULTIPLIER - credit_estimate * CONTRACT_MULTIPLIER
    if max_loss_per_contract <= 1.0:
        logger.warning("%s: max loss per contract too small (%.2f), skipping", symbol, max_loss_per_contract)
        return

    contracts = int((equity * settings.credit_spread_risk_pct_per_trade) // max_loss_per_contract)
    if contracts > settings.credit_spread_max_contracts_per_trade:
        logger.info("%s: risk-sized %d contracts, capping at %d", symbol, contracts, settings.credit_spread_max_contracts_per_trade)
        contracts = settings.credit_spread_max_contracts_per_trade
    if contracts <= 0:
        logger.info("%s: computed 0 contracts, skipping", symbol)
        return

    # Pending row first: the short legs are intended from the moment they can
    # fill (ORB's short-option guard checks every 5s).
    pid = journal.open_condor_position(
        symbol, datetime.now(timezone.utc).isoformat(), legs.expiry,
        legs.call_short.strike, legs.call_long.strike, legs.put_short.strike, legs.put_long.strike,
        legs.call_short.conId, legs.call_long.conId, legs.put_short.conId, legs.put_long.conId,
        credit_estimate, contracts, status="pending",
    )
    filled_n, credit_sum = 0, 0.0
    for limit in condor_open_ladder(ib, legs):
        trade, _ = open_condor(ib, symbol, legs, contracts - filled_n, limit)
        if not wait_for_fill(ib, trade, timeout=60):
            ib.cancelOrder(trade.order)
            ib.sleep(2)
        got = int(trade.orderStatus.filled)
        if got:
            credit_sum += abs(trade.orderStatus.avgFillPrice) * got
            filled_n += got
        if filled_n == contracts:
            break
    if filled_n == 0:
        journal.cancel_condor(pid, "not_filled")
        logger.warning("%s: open order did not fill at mid or halfway to natural — will retry next run", symbol)
        return
    contracts = filled_n
    actual_credit = credit_sum / filled_n
    journal.confirm_condor(pid, datetime.now(timezone.utc).isoformat(), actual_credit, contracts)
    journal.log_order(symbol, "OPEN_CONDOR", contracts, "COMBO", broker="IBKR", status="filled")
    logger.info("%s: OPENED condor, %d contracts, actual credit=%.2f, expiry %s", symbol, contracts, actual_credit, legs.expiry)


def main() -> None:
    journal = Journal(settings.db_path)

    with IBKRClient(settings, client_id=settings.condor_client_id) as client:
        equity = client.net_liquidation()
        logger.info("Equity: %.2f", equity)

        for symbol in settings.credit_spread_symbol_list:
            try:
                manage_open_positions(client.ib, journal, symbol)
                maybe_open_new_position(client.ib, journal, symbol, equity)
            except Exception:
                logger.exception("%s: error during condor management, skipping", symbol)


if __name__ == "__main__":
    main()
