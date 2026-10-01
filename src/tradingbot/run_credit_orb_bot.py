"""Strategy #6 — ORB credit spreads on SPY 0DTE (paper experiment).

Rules (options/credit_orb.py, from Option Alpha / Quantish SPX research):
  - Opening range: the first 60 minutes (9:30-10:30).
  - Entry: the first completed 5-min bar closing outside the range between
    10:30 and 12:00, on a day whose range is at least 0.2% of the open.
  - Breakout up -> SELL a put spread, short put at/below the range low;
    breakout down -> SELL a call spread, short call at/above the range high.
    $2 wide. One trade a day.
  - Exit: hold, then buy back at 3:50pm (SPY options settle in shares, so
    nothing is left to expire; the 3:57pm expiry guard backs this up).
  - Size: contracts so the maximum loss (width - credit) stays within
    CREDIT_ORB_MAX_LOSS_DOLLARS (default $500).

It also records both legs' real bid/ask every ~5 minutes while a spread is
open (journal credit_orb_marks) — real option-smile data the backtests lack.

Safety:
  - The journal row is written as 'pending' BEFORE the order is sent, so
    the short leg is an intended short from the moment it can fill (the
    ORB bot buys back any other short SPY option every minute).
  - Never trades a contract that already has a position (IBKR nets
    positions per contract, which would mix two strategies).
  - Closing never buys back more than the account is actually short.
  - Own client ID (CREDIT_ORB_CLIENT_ID); paper-only guard as every bot.

    python -m tradingbot.run_credit_orb_bot
"""
from __future__ import annotations

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
from ib_async import LimitOrder, Stock

from tradingbot.config import settings
from tradingbot.ibkr.client import IBKRClient
from tradingbot.ibkr.data import fetch_intraday_bars
from tradingbot.journal import Journal
from tradingbot.options.credit_orb import CONTRACT_MULTIPLIER, CreditOrbConfig, contracts_for, find_breakout, spread_strikes
from tradingbot.options.live_chain import get_quote, get_real_chain, nearest_expiry, qualify_option
from tradingbot.options.live_condor import _combo_contract, wait_for_fill
from tradingbot.position_guard import held_qty
from tradingbot.strategy.trend_join import completed_bars

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("ib_async").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
MARKET_CLOSE = time(16, 0)
TICK_SECONDS = 30.0
MARK_EVERY_SECONDS = 300.0
SOURCE = "run_credit_orb_bot"


def _now_et() -> datetime:
    return datetime.now(ET)


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def config_from_settings() -> CreditOrbConfig:
    skip = tuple(int(x) for x in settings.credit_orb_skip_weekdays.split(",") if x.strip())
    return CreditOrbConfig(
        range_minutes=settings.credit_orb_range_minutes, entry_end=_hhmm(settings.credit_orb_entry_end),
        min_range_pct=settings.credit_orb_min_range_pct, width=settings.credit_orb_width,
        anchor=settings.credit_orb_anchor, max_loss_dollars=settings.credit_orb_max_loss_dollars,
        max_contracts=settings.credit_orb_max_contracts, skip_weekdays=skip,
    )


def _leg_quotes(ib, short, long) -> tuple[float, float, float, float]:
    sb, sa, _ = get_quote(ib, short)
    lb, la, _ = get_quote(ib, long)
    return sb, sa, lb, la


def open_spread(ib, journal: Journal, cfg: CreditOrbConfig, sig, chain, symbol: str) -> dict | None:
    right, short_k, long_k = spread_strikes(sig, cfg)
    today = _now_et().date()
    expiry = nearest_expiry(chain, 0, today)
    if expiry != today.strftime("%Y%m%d"):
        logger.info("%s: no same-day expiry today (nearest %s) — skipping", symbol, expiry)
        return None
    try:
        short = qualify_option(ib, symbol, expiry, short_k, right)
        long = qualify_option(ib, symbol, expiry, long_k, right)
    except ValueError as e:
        logger.warning("%s: could not qualify spread legs: %s", symbol, e)
        return None
    if held_qty(ib, short.conId) != 0 or held_qty(ib, long.conId) != 0:
        logger.warning("%s: a leg (%s/%s) already has a position (another strategy) — skipping",
                       symbol, short.localSymbol, long.localSymbol)
        return None

    sb, sa, lb, la = _leg_quotes(ib, short, long)
    mid_credit = (sb + sa) / 2 - (lb + la) / 2
    if mid_credit < settings.credit_orb_min_credit:
        logger.info("%s: credit %.2f below minimum %.2f — skipping", symbol, mid_credit, settings.credit_orb_min_credit)
        return None
    n = contracts_for(mid_credit, cfg)
    if n == 0:
        logger.info("%s: sized to 0 contracts (credit %.2f, width %.1f)", symbol, mid_credit, cfg.width)
        return None

    pid = journal.open_credit_orb(symbol, today.isoformat(), sig.direction, right, short_k, long_k,
                                  short.conId, long.conId, n, mid_credit, sig.range_low, sig.range_high)
    journal.log_signal(symbol, f"SELL_{right}_SPREAD", sig.spot,
                       f"ORB {sig.direction} breakout, range {sig.range_low:.2f}-{sig.range_high:.2f}", SOURCE)
    combo = _combo_contract(symbol, [(short, "SELL"), (long, "BUY")])
    filled_qty, credit_fill = 0, 0.0
    for attempt, concession in enumerate((0.0, 0.02)):
        want = n - filled_qty
        limit = round(mid_credit - concession, 2)
        trade = ib.placeOrder(combo, LimitOrder("BUY", want, -limit, tif="DAY"))  # negative = net credit
        logger.info("%s: SELL %s%s/%s%s x%d for credit %.2f (attempt %d)",
                    symbol, short_k, right, long_k, right, want, limit, attempt + 1)
        done = wait_for_fill(ib, trade, timeout=60)
        if not done:
            ib.cancelOrder(trade.order)
            ib.sleep(2)
        got = int(trade.orderStatus.filled)
        if got:
            credit_fill = (credit_fill * filled_qty + abs(trade.orderStatus.avgFillPrice) * got) / (filled_qty + got)
            filled_qty += got
        if filled_qty >= n:
            break
    if filled_qty == 0:
        journal.cancel_credit_orb(pid, "not_filled")
        logger.warning("%s: credit spread not filled — no trade today", symbol)
        return None
    journal.confirm_credit_orb(pid, filled_qty, credit_fill)
    journal.log_order(symbol, f"SELL_{right}_SPREAD", filled_qty, "BAG", broker="IBKR", status="filled")
    logger.info("%s: OPENED credit spread %s%s/%s%s x%d, credit %.2f, max loss $%.0f",
                symbol, short_k, right, long_k, right, filled_qty, credit_fill,
                (cfg.width - credit_fill) * filled_qty * CONTRACT_MULTIPLIER)
    return {"id": pid, "short": short, "long": long, "contracts": filled_qty, "credit": credit_fill,
            "right": right, "close_attempts": 0}


def close_spread(ib, journal: Journal, pos: dict, symbol: str, reason: str) -> bool:
    """Buy the spread back. Returns True once flat. Never buys back more
    than the account is actually short on the short leg."""
    short_held = -held_qty(ib, pos["short"].conId)
    qty = max(0, min(pos["contracts"], int(short_held)))
    if qty == 0:
        journal.close_credit_orb(pos["id"], 0.0, "already_flat", pos.get("realized", 0.0))
        logger.warning("%s: credit spread already flat at IBKR — marking closed", symbol)
        return True
    sb, sa, lb, la = _leg_quotes(ib, pos["short"], pos["long"])
    value = max(0.01, (sb + sa) / 2 - (lb + la) / 2)
    limit = round(value + 0.02 * (1 + pos["close_attempts"]), 2)  # more aggressive on each retry
    combo = _combo_contract(symbol, [(pos["short"], "BUY"), (pos["long"], "SELL")])
    trade = ib.placeOrder(combo, LimitOrder("BUY", qty, limit, tif="DAY"))  # positive = net debit
    done = wait_for_fill(ib, trade, timeout=45)
    if not done:
        ib.cancelOrder(trade.order)
        ib.sleep(2)
    got = int(trade.orderStatus.filled)
    pos["close_attempts"] += 1
    if got:
        cost = abs(trade.orderStatus.avgFillPrice)
        pos["realized"] = pos.get("realized", 0.0) + (pos["credit"] - cost) * got * CONTRACT_MULTIPLIER
        pos["contracts"] -= got
        pos["last_cost"] = cost
        journal.update_credit_orb_contracts(pos["id"], pos["contracts"])
    if pos["contracts"] > 0:
        logger.warning("%s: close filled %d of %d — %d left, retrying", symbol, got, qty, pos["contracts"])
        return False
    journal.close_credit_orb(pos["id"], pos.get("last_cost", 0.0), reason, pos["realized"])
    journal.log_order(symbol, "CLOSE_CREDIT_SPREAD", qty, "BAG", broker="IBKR", status="filled")
    logger.info("%s: CLOSED credit spread (%s), cost %.2f, P&L %.2f", symbol, reason, pos.get("last_cost", 0.0),
                pos["realized"])
    return True


def record_mark(ib, journal: Journal, pos: dict, underlying) -> None:
    try:
        sb, sa, lb, la = _leg_quotes(ib, pos["short"], pos["long"])
        _, _, spot = get_quote(ib, underlying)
        journal.record_credit_orb_mark(pos["id"], spot, sb, sa, lb, la)
        logger.info("Spread mark: short %.2f/%.2f long %.2f/%.2f (value %.2f vs credit %.2f), spot %.2f",
                    sb, sa, lb, la, (sb + sa) / 2 - (lb + la) / 2, pos["credit"], spot)
    except Exception:
        logger.exception("Mark failed")


def recover(ib, journal: Journal) -> dict | None:
    from ib_async import Option
    today = _now_et().date().isoformat()
    for row in journal.open_credit_orb_rows():
        if row["status"] == "pending":
            journal.cancel_credit_orb(row["id"], "pending_at_restart")
            continue
        if row["trade_date"] != today:
            # 0DTE: an earlier day's spread has expired (or the expiry guard
            # closed it). Don't try to rehydrate an expired contract.
            journal.close_credit_orb(row["id"], 0.0, "stale_from_previous_day", row["realized_pnl"] or 0.0)
            logger.warning("Credit spread id=%s from %s left open in the journal — marked closed; "
                           "check the snapshot's closed-trade P&L for the real result", row["id"], row["trade_date"])
            continue
        legs = []
        for con_id in (row["short_conid"], row["long_conid"]):
            q = ib.qualifyContracts(Option(conId=con_id, exchange="SMART"))
            legs.append(q[0])
        logger.warning("Recovered open credit spread id=%s x%d", row["id"], row["contracts"])
        return {"id": row["id"], "short": legs[0], "long": legs[1], "contracts": row["contracts"],
                "credit": row["credit"], "right": row["right"], "close_attempts": 0}
    return None


def main() -> None:
    if not settings.credit_orb_enabled:
        logger.info("CREDIT_ORB_ENABLED=false — nothing to do")
        return
    cfg = config_from_settings()
    symbol = settings.credit_orb_symbol
    close_time = _hhmm(settings.credit_orb_close_time)
    journal = Journal(settings.db_path)
    if _now_et().weekday() in cfg.skip_weekdays:
        logger.info("Skipping today (weekday %d in CREDIT_ORB_SKIP_WEEKDAYS)", _now_et().weekday())
        return

    with IBKRClient(settings, client_id=settings.credit_orb_client_id) as client:
        ib = client.ib
        underlying = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(underlying)
        chain = get_real_chain(ib, symbol)
        pos = recover(ib, journal)
        traded = journal.credit_orb_traded_on(_now_et().date().isoformat())
        last_bar, last_mark = None, 0.0
        logger.info("Credit ORB loop starting (%s, range %dm, entries until %s, max loss $%.0f)",
                    symbol, cfg.range_minutes, settings.credit_orb_entry_end, cfg.max_loss_dollars)

        while _now_et().time() < MARKET_CLOSE:
            now = _now_et()
            try:
                if pos is not None:
                    if now.time() >= close_time:
                        if close_spread(ib, journal, pos, symbol, "close_1550"):
                            pos = None
                    elif now.timestamp() - last_mark >= MARK_EVERY_SECONDS:
                        record_mark(ib, journal, pos, underlying)
                        last_mark = now.timestamp()
                elif not traded and now.time() < cfg.entry_end:
                    bars = completed_bars(
                        fetch_intraday_bars(ib, symbol, cfg.bar_minutes, duration="1 D"), pd.Timestamp(now), cfg.bar_minutes,
                    )
                    day = bars[bars.index.date == now.date()]
                    if not day.empty and day.index[-1] != last_bar:
                        last_bar = day.index[-1]
                        sig = find_breakout(day, cfg)
                        # Act only on a fresh breakout (this bar or the one before), never a stale one.
                        if sig is not None and sig.bar_ts >= day.index[-1] - pd.Timedelta(minutes=cfg.bar_minutes):
                            logger.info("%s: ORB %s breakout (range %.2f-%.2f, close %.2f)",
                                        symbol, sig.direction, sig.range_low, sig.range_high, sig.spot)
                            pos = open_spread(ib, journal, cfg, sig, chain, symbol)
                            traded = True
                            last_mark = 0.0
                        elif sig is None:
                            logger.info("%s: no qualifying breakout yet (%s)", symbol, last_bar.strftime("%H:%M"))
            except Exception:
                logger.exception("Credit ORB tick failed — retrying next tick")
            ib.sleep(TICK_SECONDS)

        if pos is not None:
            logger.error("Credit spread still open at the close — expiry guard / manual check needed")
        logger.info("Credit ORB loop finished for today")


if __name__ == "__main__":
    main()
