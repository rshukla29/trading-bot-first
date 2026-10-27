"""Long-running intraday entrypoint for strategy #7, Stock ORB — the
published rules of the day-trader-skill pack (strategy/stock_orb.py),
run as a paper experiment from 2026-10-27.

Daily flow (all times ET):
  every 5-min bar, 9:45-15:30  ~20s after the bar closes: Yahoo 5-min bars
                               for the S&P 500; a symbol whose JUST-completed
                               bar is its first close above the 15-min
                               opening range high is a hit. Hits are ranked
                               by dollars traded so far, re-checked on IBKR's
                               own bars, and bought with a bracket: market
                               BUY + stop at the range midpoint + limit SELL
                               at 2R (one-cancels-other, all at IBKR)
  after each entry             Time Machine report (logged + journaled only)
  every ~15s                   watch for the stop or target filling
  15:55                        flatten whatever is left, then exit

Limits (the pack's trader-risk defaults, unscaled): 3 trades a day, $50
risk per trade, $150 of stop-risk a day, $4,000 per position, long only,
one entry per symbol per day.

Ownership: never trades a symbol on the MA-crossover / RSI(2) watchlists,
any symbol with an IBKR position (that covers manual positions like ASTX
and Trend Join's), or one Trend Join entered today. Orders can only be
changed by the client that placed them, so stock_orb_client_id stays fixed.

Refuses to trade on a non-paper account unless LIVE_TRADING=true.

    python -m tradingbot.run_stock_orb_bot
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from ib_async import IB, LimitOrder, MarketOrder, Stock, StopOrder, Trade

from tradingbot.config import settings
from tradingbot.ibkr.client import IBKRClient
from tradingbot.ibkr.data import fetch_intraday_bars
from tradingbot.journal import Journal
from tradingbot.options.live_orb import wait_for_fill
from tradingbot.position_guard import clamp_sell_qty, held_qty
from tradingbot.sp500_scanner import fetch_sp500_constituents
from tradingbot.strategy.stock_orb import (
    Trigger,
    find_trigger,
    plan_trade,
    regular_session,
    share_quantity,
    time_machine,
)
from tradingbot.strategy.trend_join import completed_bars

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("ib_async").setLevel(logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
TICK_SECONDS = 15.0
SCAN_DELAY_SECONDS = 20.0  # after each 5-min boundary, for Yahoo to finish the bar
MARKET_CLOSE = time(16, 0)
SOURCE = "run_stock_orb_bot"


def _now_et() -> datetime:
    return datetime.now(ET)


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def ib_symbol(yahoo_symbol: str) -> str:
    return yahoo_symbol.replace("-", " ")


def yahoo_symbol(ib_sym: str) -> str:
    return ib_sym.replace(" ", "-")


@dataclass
class LivePosition:
    id: int
    symbol: str
    contract: Stock
    entry: float
    qty: int
    stop: float
    target: float
    stop_trade: Trade | None
    target_trade: Trade | None


# --------------------------------------------------------------------------
# Scan / entry
# --------------------------------------------------------------------------

def fresh_trigger(day: pd.DataFrame, last_completed: pd.Timestamp) -> Trigger | None:
    """The day's first trigger, only if it is the bar that just completed —
    the pack's entry timer fires at the trigger bar's close; a later bar
    would be a different (chased) entry price."""
    trig = find_trigger(day, settings.stock_orb_or_minutes, settings.stock_orb_bar_minutes,
                        _hhmm(settings.stock_orb_entry_start), _hhmm(settings.stock_orb_entry_end))
    return trig if trig is not None and trig.ts == last_completed else None


def yahoo_hits(symbols: list[str], now: pd.Timestamp) -> list[tuple[str, Trigger]]:
    """Fresh triggers on Yahoo's 5-min bars, most dollars traded first."""
    bm = settings.stock_orb_bar_minutes
    raw = yf.download(symbols, period="1d", interval="5m", group_by="ticker", auto_adjust=False,
                      prepost=False, threads=True, progress=False)
    last_completed = now.floor(f"{bm}min") - pd.Timedelta(minutes=bm)
    hits = []
    for s in symbols:
        if raw is None or raw.empty or s not in raw.columns.get_level_values(0):
            continue
        df = raw[s].rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
        if df.empty:
            continue
        df.index = df.index.tz_convert(ET)
        day = completed_bars(regular_session(df), now, bm)
        day = day[day.index.date == now.date()]
        if day.empty or day["close"].iloc[-1] < settings.stock_orb_min_price:
            continue
        trig = fresh_trigger(day, last_completed)
        if trig is not None:
            hits.append((s, trig))
    return sorted(hits, key=lambda h: -h[1].dollar_volume)


def excluded_symbols(client: IBKRClient, journal: Journal, today: str) -> set[str]:
    held = {p.contract.symbol for p in client.positions() if p.position != 0 and p.contract.secType == "STK"}
    return (set(settings.symbols) | set(settings.rsi2_symbols) | held
            | {r["symbol"] for r in journal.stock_orb_rows_on(today)}
            | set(journal.trend_join_symbols_traded_on(today)))


def risk_used_today(journal: Journal, today: str) -> float:
    return sum(r["qty"] * (r["entry_price"] - r["stop_price"]) for r in journal.stock_orb_rows_on(today))


def place_bracket(ib: IB, contract: Stock, qty: int, stop: float, target: float) -> tuple[Trade, Trade, Trade]:
    """Market BUY + DAY stop + DAY target, the two exits one-cancels-other,
    transmitted together so the shares are never unprotected."""
    parent = MarketOrder("BUY", qty, tif="DAY")
    parent.orderId = ib.client.getReqId()
    parent.transmit = False
    oca = f"stock_orb_{parent.orderId}"
    take = LimitOrder("SELL", qty, target, tif="DAY")
    take.orderId = ib.client.getReqId()
    take.parentId = parent.orderId
    take.ocaGroup, take.ocaType = oca, 1
    take.transmit = False
    stop_order = StopOrder("SELL", qty, stop, tif="DAY")
    stop_order.orderId = ib.client.getReqId()
    stop_order.parentId = parent.orderId
    stop_order.ocaGroup, stop_order.ocaType = oca, 1
    stop_order.transmit = True
    return ib.placeOrder(contract, parent), ib.placeOrder(contract, take), ib.placeOrder(contract, stop_order)


def try_enter(ib: IB, journal: Journal, yahoo_sym: str, now: pd.Timestamp, today: str) -> LivePosition | None:
    sym = ib_symbol(yahoo_sym)
    bm = settings.stock_orb_bar_minutes
    # Re-check on IBKR's own bars: a Yahoo data quirk must not trade by itself.
    bars = completed_bars(fetch_intraday_bars(ib, sym, bm, duration="1 D", use_rth=True), now, bm)
    day = bars[bars.index.date == now.date()]
    last_completed = now.floor(f"{bm}min") - pd.Timedelta(minutes=bm)
    trig = fresh_trigger(day, last_completed) if not day.empty else None
    if trig is None:
        logger.info("%s: Yahoo hit not confirmed on IBKR bars — skipping", sym)
        return None
    plan = plan_trade(trig.entry, trig.or_high, trig.or_low, settings.stock_orb_target_r)
    if plan is None:
        logger.info("%s: risk to the range midpoint is inside the spread — skipping", sym)
        return None
    qty = share_quantity(plan.entry, plan.stop, settings.stock_orb_max_loss_per_trade,
                         settings.stock_orb_max_order_value)
    risk_dollars = qty * plan.risk
    if qty <= 0:
        return None
    if risk_used_today(journal, today) + risk_dollars > settings.stock_orb_max_daily_loss:
        logger.info("%s: daily loss budget ($%.0f) would be exceeded — skipping", sym, settings.stock_orb_max_daily_loss)
        return None

    reason = (f"close {trig.entry:.2f} > opening range high {trig.or_high:.2f} "
              f"(range {trig.or_low:.2f}-{trig.or_high:.2f}, bar {trig.ts.strftime('%H:%M')})")
    journal.log_signal(sym, "BUY", trig.entry, reason, source=SOURCE)
    contract = Stock(sym, "SMART", "USD")
    ib.qualifyContracts(contract)
    parent, take, stop_trade = place_bracket(ib, contract, qty, plan.stop, plan.target)
    if not wait_for_fill(ib, parent):
        logger.warning("%s: entry did not fill (status=%s), cancelling", sym, parent.orderStatus.status)
        ib.cancelOrder(parent.order)
        return None
    fill = parent.orderStatus.avgFillPrice
    journal.log_order(sym, "BUY", qty, "MKT+STP+LMT", broker="IBKR", status="filled", stop_price=plan.stop,
                      broker_order_id=str(parent.order.orderId))
    pid = journal.open_stock_orb_position(
        sym, today, trig.ts.strftime("%H:%M"), trig.or_high, trig.or_low, fill, qty, plan.stop, plan.target,
        stop_trade.order.orderId, take.order.orderId,
    )
    pos = LivePosition(pid, sym, contract, fill, qty, plan.stop, plan.target, stop_trade, take)
    logger.info("%s: BOUGHT %d @ %.2f (signal %.2f), stop %.2f, target %.2f (%.1fR), risk $%.0f — %s",
                sym, qty, fill, trig.entry, plan.stop, plan.target, settings.stock_orb_target_r,
                qty * (fill - plan.stop), reason)
    if fill <= plan.stop:
        logger.warning("%s: filled at/below the stop, flattening immediately", sym)
        flatten(ib, journal, pos, "fill_below_stop")
        return None
    if settings.stock_orb_time_machine:
        report_time_machine(journal, pos, plan, trig, now)
    return pos


def report_time_machine(journal: Journal, pos: LivePosition, plan, trig: Trigger, now: pd.Timestamp) -> None:
    """Report-only: never blocks or changes a trade."""
    try:
        raw = yf.download(yahoo_symbol(pos.symbol), period="60d", interval="5m", auto_adjust=False,
                          prepost=False, progress=False, multi_level_index=False)
        hist = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
        hist.index = hist.index.tz_convert(ET)
        bm = settings.stock_orb_bar_minutes
        slot = ((trig.ts.hour - 9) * 60 + trig.ts.minute - 30) // bm + 1
        rep = time_machine(hist, plan, slot, settings.stock_orb_target_r, settings.stock_orb_or_minutes,
                           today=now)
        if rep is None:
            journal.set_stock_orb_time_machine(pos.id, "Time Machine: fewer than 20 full sessions", None, None, None)
            return
        logger.info("%s: %s", pos.symbol, rep.summary())
        journal.set_stock_orb_time_machine(pos.id, rep.summary(), rep.mc_avg_r, rep.hist_trades, rep.hist_avg_r)
    except Exception:
        logger.exception("%s: Time Machine report failed (report-only, trade unaffected)", pos.symbol)


# --------------------------------------------------------------------------
# Position management
# --------------------------------------------------------------------------

def _filled(t: Trade | None) -> int:
    return int(t.orderStatus.filled) if t is not None else 0


def check_exits(journal: Journal, pos: LivePosition) -> bool:
    """True once the stop or the target has fully filled at IBKR."""
    for trade, reason in ((pos.stop_trade, "stop"), (pos.target_trade, "target")):
        if trade is not None and trade.orderStatus.status == "Filled":
            px = trade.orderStatus.avgFillPrice or (pos.stop if reason == "stop" else pos.target)
            other = pos.target_trade if reason == "stop" else pos.stop_trade
            # A partial fill on the other leg before the OCA cancel is rare but possible.
            other_qty, other_px = _filled(other), (other.orderStatus.avgFillPrice if other is not None else 0.0)
            pnl = (px - pos.entry) * (pos.qty - other_qty) + (other_px - pos.entry) * other_qty
            journal.close_stock_orb_position(pos.id, px, reason, pnl)
            journal.log_order(pos.symbol, "SELL", pos.qty, "STP" if reason == "stop" else "LMT", broker="IBKR",
                              status="filled")
            logger.info("%s: %s hit, sold %d @ %.2f, trade P&L %.2f", pos.symbol, reason.upper(), pos.qty, px, pnl)
            return True
    return False


def flatten(ib: IB, journal: Journal, pos: LivePosition, reason: str) -> bool:
    for t in (pos.stop_trade, pos.target_trade):
        if t is not None and t.orderStatus.status not in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
            ib.cancelOrder(t.order)
    ib.sleep(2)
    if check_exits(journal, pos):  # filled while we were cancelling
        return True
    done = _filled(pos.stop_trade) + _filled(pos.target_trade)
    qty = clamp_sell_qty(pos.qty - done, held_qty(ib, pos.contract.conId))
    pnl = sum((t.orderStatus.avgFillPrice - pos.entry) * _filled(t) for t in (pos.stop_trade, pos.target_trade)
              if t is not None and _filled(t))
    if qty == 0:
        journal.close_stock_orb_position(pos.id, pos.entry, "already_flat", pnl)
        logger.warning("%s: nothing left at IBKR to sell — marking closed", pos.symbol)
        return True
    trade = ib.placeOrder(pos.contract, MarketOrder("SELL", qty, tif="DAY"))
    if not wait_for_fill(ib, trade):
        logger.error("%s: flatten SELL %d did not fill (status=%s) — re-placing the stop, will retry",
                     pos.symbol, qty, trade.orderStatus.status)
        ib.cancelOrder(trade.order)
        pos.stop_trade = ib.placeOrder(pos.contract, StopOrder("SELL", qty, pos.stop, tif="DAY"))
        pos.target_trade = None
        return False
    px = trade.orderStatus.avgFillPrice
    pnl += (px - pos.entry) * qty
    journal.close_stock_orb_position(pos.id, px, reason, pnl)
    journal.log_order(pos.symbol, "SELL", qty, "MKT", broker="IBKR", status="filled")
    logger.info("%s: CLOSED %d @ %.2f (%s), trade P&L %.2f", pos.symbol, qty, px, reason, pnl)
    return True


def recover_positions(ib: IB, client: IBKRClient, journal: Journal) -> list[LivePosition]:
    held = {p.contract.symbol: int(p.position) for p in client.positions() if p.contract.secType == "STK"}
    ib.reqOpenOrders()
    open_trades = {t.order.orderId: t for t in ib.openTrades()}
    out = []
    for row in journal.open_stock_orb_positions():
        sym = row["symbol"]
        contract = Stock(sym, "SMART", "USD")
        ib.qualifyContracts(contract)
        pos = LivePosition(row["id"], sym, contract, row["entry_price"], row["qty"], row["stop_price"],
                           row["target_price"], open_trades.get(row["stop_order_id"]),
                           open_trades.get(row["target_order_id"]))
        shares = held.get(sym, 0)
        if shares <= 0:
            journal.close_stock_orb_position(pos.id, pos.stop, "closed_while_offline",
                                             (pos.stop - pos.entry) * pos.qty)
            logger.warning("%s: no longer held — marking closed (P&L estimated at the stop)", sym)
            continue
        pos.qty = min(pos.qty, shares)
        if pos.stop_trade is None:
            pos.stop_trade = ib.placeOrder(contract, StopOrder("SELL", pos.qty, pos.stop, tif="DAY"))
            logger.warning("%s: stop missing on recovery — re-placed @ %.2f", sym, pos.stop)
        logger.warning("%s: recovered open position (%d @ %.2f, stop %.2f, target %.2f)",
                       sym, pos.qty, pos.entry, pos.stop, pos.target)
        out.append(pos)
    return out


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def main() -> None:
    if not settings.stock_orb_enabled:
        logger.info("Stock ORB disabled (STOCK_ORB_ENABLED=false)")
        return
    journal = Journal(settings.db_path)
    entry_start, entry_end = _hhmm(settings.stock_orb_entry_start), _hhmm(settings.stock_orb_entry_end)
    flatten_time = _hhmm(settings.stock_orb_flatten_time)
    bm = settings.stock_orb_bar_minutes
    universe = fetch_sp500_constituents()["symbol"].tolist()

    with IBKRClient(settings, client_id=settings.stock_orb_client_id) as client:
        ib = client.ib
        ib.reqMarketDataType(3)
        positions = recover_positions(ib, client, journal)
        last_scan = None
        logger.info("Stock ORB live loop starting (%d recovered positions, %d symbols)", len(positions), len(universe))

        while _now_et().time() < MARKET_CLOSE:
            now = pd.Timestamp(_now_et())
            today = now.date().isoformat()

            for pos in list(positions):
                try:
                    if check_exits(journal, pos) or (now.time() >= flatten_time and flatten(ib, journal, pos, "eod_flatten")):
                        positions.remove(pos)
                except Exception:
                    logger.exception("%s: error managing position (will retry)", pos.symbol)

            boundary = now.floor(f"{bm}min")
            bar_end = boundary.time()  # the bar that just completed ends here
            if (boundary != last_scan and (now - boundary).total_seconds() >= SCAN_DELAY_SECONDS
                    and entry_start <= bar_end <= entry_end):
                last_scan = boundary
                if len(journal.stock_orb_rows_on(today)) < settings.stock_orb_max_trades_per_day:
                    try:
                        excluded = excluded_symbols(client, journal, today)
                        hits = yahoo_hits([s for s in universe if ib_symbol(s) not in excluded], now)
                        logger.info("Bar %s: %d fresh breakout(s)%s", (boundary - pd.Timedelta(minutes=bm)).strftime("%H:%M"),
                                    len(hits), f": {[h[0] for h in hits[:10]]}" if hits else "")
                        for ysym, _ in hits:
                            if len(journal.stock_orb_rows_on(today)) >= settings.stock_orb_max_trades_per_day:
                                logger.info("Max trades/day (%d) reached", settings.stock_orb_max_trades_per_day)
                                break
                            pos = try_enter(ib, journal, ysym, now, today)
                            if pos is not None:
                                positions.append(pos)
                    except Exception:
                        logger.exception("Scan failed — will retry on the next bar")

            if now.time() >= flatten_time and not positions:
                break
            ib.sleep(TICK_SECONDS)

        if positions:
            logger.error("Still holding %s at the close — flatten failed; the DAY stops expire at the close, "
                         "so these need manual attention", [p.symbol for p in positions])
        logger.info("Stock ORB live loop finished for today")


if __name__ == "__main__":
    main()
