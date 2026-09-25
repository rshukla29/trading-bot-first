from __future__ import annotations

import logging
from datetime import time

from ib_async import IB, LimitOrder, Option, Trade

from tradingbot.options.live_chain import get_quote, nearest_expiry, qualify_nearest_available

logger = logging.getLogger(__name__)

CONTRACT_MULTIPLIER = 100

# Matches options/backtest.py's default: flat 5 minutes before the 4:00pm
# ET close, not right at it — options can get illiquid/hard-to-fill in the
# closing minutes, same reasoning as the iron condor's 3:45pm schedule.
EOD_FLATTEN_TIME = time(15, 55)


def select_orb_contract(
    ib: IB, symbol: str, chain, spot: float, right: str, otm_pct: float, strike_increment: float, dte_days: int,
) -> Option:
    """Real, listed contract nearest the target strike/expiry. right: 'C' or 'P'."""
    expiry = nearest_expiry(chain, dte_days)
    direction = 1 if right == "C" else -1
    raw_strike = spot * (1 + direction * otm_pct)
    target_strike = round(raw_strike / strike_increment) * strike_increment
    return qualify_nearest_available(ib, symbol, expiry, target_strike, right, chain)


def determine_exit_reason(
    now_time: time,
    current_premium: float,
    entry_premium: float,
    stop_loss_pct_of_premium: float,
    profit_target_pct: float | None,
    spot: float,
    right: str,
    range_low: float,
    range_high: float,
    exit_on_range_reentry: bool,
    eod_flatten_time: time = EOD_FLATTEN_TIME,
    step_stop_premium: float | None = None,
) -> str | None:
    """Pure decision logic. Priority: eod_flatten > stop_loss > step_stop >
    profit_target > range_reentry. Everything except step_stop mirrors
    options/backtest.py; step_stop (10% step profit lock on premium) is
    live-only and disabled when step_stop_premium is None. Returns the
    exit reason, or None to keep holding."""
    if now_time >= eod_flatten_time:
        return "eod_flatten"
    if current_premium <= entry_premium * (1 - stop_loss_pct_of_premium):
        return "stop_loss"
    # 10% step profit lock (risk.step_trailing_stop, computed from the peak
    # premium): once the option has been up +11%, falling back to +10% exits.
    if step_stop_premium is not None and current_premium <= step_stop_premium:
        return "step_stop"
    if profit_target_pct is not None and current_premium >= entry_premium * (1 + profit_target_pct):
        return "profit_target"
    if exit_on_range_reentry:
        if right == "C" and spot <= range_high:
            return "range_reentry"
        if right == "P" and spot >= range_low:
            return "range_reentry"
    return None


def enter_orb_trade(ib: IB, contract: Option, contracts: int) -> tuple[Trade, float]:
    """Buys to open at the live mid. Returns (Trade, mid_price_used)."""
    _, _, mid = get_quote(ib, contract)
    # tif must be set explicitly, same reasoning as live_condor.py: LimitOrder
    # defaults tif='' (unset), which triggers an unreliable IBKR auto-correct
    # cycle that can leave the order stuck Cancelled instead of working.
    order = LimitOrder("BUY", contracts, round(mid, 2), tif="DAY")
    trade = ib.placeOrder(contract, order)
    logger.info("Opened ORB trade: %s x%d @ ~%.2f", contract.localSymbol, contracts, mid)
    return trade, mid


def exit_orb_trade(ib: IB, contract: Option, contracts: int) -> tuple[Trade, float]:
    """Sells to close at the live mid. Returns (Trade, mid_price_used)."""
    _, _, mid = get_quote(ib, contract)
    order = LimitOrder("SELL", contracts, round(mid, 2), tif="DAY")
    trade = ib.placeOrder(contract, order)
    logger.info("Closed ORB trade: %s x%d @ ~%.2f", contract.localSymbol, contracts, mid)
    return trade, mid


def wait_for_fill(ib: IB, trade: Trade, timeout: float = 45.0, poll_interval: float = 3.0) -> bool:
    """Single-leg orders fill markedly faster than the 4-leg combos in
    live_condor.py, so a shorter timeout/poll here is appropriate — but
    this is a long-running intraday loop, not a once-daily batch job, so
    still bounded rather than indefinite."""
    elapsed = 0.0
    while elapsed < timeout:
        if trade.orderStatus.status == "Filled":
            return True
        if trade.orderStatus.status in ("Cancelled", "ApiCancelled", "Inactive"):
            return False
        ib.sleep(poll_interval)
        elapsed += poll_interval
    return trade.orderStatus.status == "Filled"
