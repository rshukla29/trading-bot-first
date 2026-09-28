"""Long-running intraday entrypoint for the "Trend Join Long" strategy
(rules and source in strategy/trend_join.py).

Daily flow (all times ET):
  09:55, 10:25 ... 12:55  prefilter: yfinance daily bars for the S&P 500,
                          keep names gapping >= 3% above yesterday's close
                          whose close was above the 200-SMA, top 20 by gap
  10:05-15:30             every completed 5-min bar: check the intraday
                          filters on each candidate via IBKR bars, enter
                          with a market order + broker-side protective stop
  every ~60s              manage open positions: +0.75R sell 1/3, +1R stop
                          to breakeven, then trail under 5-min swing lows
  15:51                   flatten everything, then exit the process

Ownership: never trades a symbol on the MA-crossover or RSI(2) watchlists,
or any symbol that already has an IBKR position, so the strategies can't
close each other's positions. Its own positions are recorded in the
journal's trend_join_positions table (run_rsi2_bot.py excludes those).

The protective stop lives at IBKR (not just in this process), so a crash
mid-day still leaves every position stopped out at the right level; on
restart, open positions and their stop orders are recovered from the
journal. Orders can only be modified by the client ID that placed them,
so trend_join_client_id must stay fixed.

Refuses to trade on a non-paper account unless LIVE_TRADING=true.

    python -m tradingbot.run_trend_join_bot
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
from ib_async import IB, MarketOrder, Stock, StopOrder, Trade

from tradingbot.config import settings
from tradingbot.ibkr.client import IBKRClient
from tradingbot.ibkr.data import fetch_intraday_bars
from tradingbot.journal import Journal
from tradingbot.options.live_chain import get_quote
from tradingbot.options.live_orb import wait_for_fill
from tradingbot.position_guard import clamp_sell_qty, held_qty
from tradingbot.sp500_scanner import download_daily_bars, fetch_sp500_constituents
from tradingbot.strategy.trend_join import (
    GapCandidate,
    PositionState,
    check_entry,
    completed_bars,
    daily_prefilter,
    initial_stop,
    latest_swing_low,
    plan_exit,
    rank_candidates,
    relative_volume,
    share_quantity,
    split_session,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("ib_async").setLevel(logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
TICK_SECONDS = 60.0
MARKET_CLOSE = time(16, 0)
SOURCE = "run_trend_join_bot"


def _now_et() -> datetime:
    return datetime.now(ET)


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def ib_symbol(yahoo_symbol: str) -> str:
    """Yahoo class-share notation (BRK-B) -> IBKR's (BRK B)."""
    return yahoo_symbol.replace("-", " ")


@dataclass
class LivePosition:
    id: int
    symbol: str
    contract: Stock
    state: PositionState
    stop_trade: Trade | None
    realized_pnl: float = 0.0


# --------------------------------------------------------------------------
# Prefilter / entry
# --------------------------------------------------------------------------

def run_prefilter(today, exclude: set[str]) -> list[GapCandidate]:
    constituents = fetch_sp500_constituents()
    symbols = [s for s in constituents["symbol"] if ib_symbol(s) not in exclude]
    bars = download_daily_bars(symbols, period="2y")
    candidates = [
        c for s, b in bars.items()
        if (c := daily_prefilter(
            s, b, today, settings.trend_join_min_gap_pct, settings.trend_join_min_price,
            settings.trend_join_trend_ma,
        )) is not None
    ]
    ranked = rank_candidates(candidates, settings.trend_join_max_candidates)
    logger.info(
        "Prefilter: %d/%d S&P 500 names passed D2+D3; watching %s",
        len(candidates), len(bars), [f"{c.symbol} +{c.gap_pct}%" for c in ranked],
    )
    return ranked


def excluded_symbols(client: IBKRClient, journal: Journal, today: str) -> set[str]:
    held = {p.contract.symbol for p in client.positions() if p.position != 0 and p.contract.secType == "STK"}
    return set(settings.symbols) | set(settings.rsi2_symbols) | held | set(journal.trend_join_symbols_traded_on(today))


def try_enter(
    ib: IB, client: IBKRClient, journal: Journal, cand: GapCandidate,
    volume_history: dict[str, pd.DataFrame], now: pd.Timestamp,
) -> LivePosition | None:
    sym = ib_symbol(cand.symbol)
    bm = settings.trend_join_bar_minutes
    if sym not in volume_history:
        volume_history[sym] = fetch_intraday_bars(ib, sym, bm, duration="20 D", use_rth=True)
    bars = completed_bars(fetch_intraday_bars(ib, sym, bm, duration="1 D", use_rth=False), now, bm)
    premarket, regular = split_session(bars, now.date())
    rvol = relative_volume(regular, volume_history[sym], settings.trend_join_rvol_lookback_days)
    check = check_entry(cand, premarket, regular, rvol, settings.trend_join_min_rvol)
    if not check.ok:
        logger.info("%s: no entry (%s)", sym, check.reason)
        return None

    stop = initial_stop(check.price, check.low_of_day, settings.trend_join_lod_stop_buffer_pct, settings.stop_loss_pct)
    qty = share_quantity(
        client.net_liquidation(), settings.risk_pct_per_trade, check.price, stop, settings.max_position_value,
    )
    if qty <= 0:
        logger.info("%s: entry signal but sized to 0 shares (price %.2f), skipping", sym, check.price)
        return None
    journal.log_signal(sym, "BUY", check.price, check.reason, source=SOURCE)

    contract = Stock(sym, "SMART", "USD")
    ib.qualifyContracts(contract)
    # Market entry with a linked protective stop, transmitted together so
    # the position is never unprotected (same pattern as ibkr/orders.py).
    parent = MarketOrder("BUY", qty, tif="DAY")
    parent.orderId = ib.client.getReqId()
    parent.transmit = False
    child = StopOrder("SELL", qty, stop, tif="DAY")
    child.orderId = ib.client.getReqId()
    child.parentId = parent.orderId
    child.transmit = True
    parent_trade = ib.placeOrder(contract, parent)
    stop_trade = ib.placeOrder(contract, child)

    if not wait_for_fill(ib, parent_trade):
        logger.warning("%s: entry did not fill (status=%s), cancelling", sym, parent_trade.orderStatus.status)
        ib.cancelOrder(parent)
        return None

    fill = parent_trade.orderStatus.avgFillPrice
    journal.log_order(sym, "BUY", qty, "MKT", broker="IBKR", status="filled", stop_price=stop,
                      broker_order_id=str(parent.orderId))
    # R is measured from the actual fill. If the fill slipped to or below
    # the stop, the trade is invalid before it started — close it out.
    position_id = journal.open_trend_join_position(
        sym, now.date().isoformat(), fill, qty, stop, child.orderId, check.reason,
    )
    pos = LivePosition(position_id, sym, contract, PositionState(fill, stop, stop, qty, qty), stop_trade)
    logger.info("%s: BOUGHT %d @ %.2f, stop %.2f (R=%.2f) — %s", sym, qty, fill, stop, fill - stop, check.reason)
    if fill <= stop:
        logger.warning("%s: filled at/below stop, flattening immediately", sym)
        flatten(ib, journal, pos, "fill_below_stop")
        return None
    return pos


# --------------------------------------------------------------------------
# Position management
# --------------------------------------------------------------------------

def _sell(ib: IB, pos: LivePosition, qty: int) -> float | None:
    qty = clamp_sell_qty(qty, held_qty(ib, pos.contract.conId))  # never sell into a short
    if qty == 0:
        logger.warning("%s: nothing held at IBKR to sell", pos.symbol)
        return None
    trade = ib.placeOrder(pos.contract, MarketOrder("SELL", qty, tif="DAY"))
    if not wait_for_fill(ib, trade):
        logger.error("%s: market SELL %d did not fill (status=%s)", pos.symbol, qty, trade.orderStatus.status)
        ib.cancelOrder(trade.order)
        return None
    return trade.orderStatus.avgFillPrice


def _modify_stop(ib: IB, pos: LivePosition, stop_price: float, qty: int) -> None:
    order = pos.stop_trade.order
    order.auxPrice = stop_price
    order.totalQuantity = qty
    ib.placeOrder(pos.contract, order)


def flatten(ib: IB, journal: Journal, pos: LivePosition, reason: str) -> bool:
    if pos.stop_trade is not None and pos.stop_trade.orderStatus.status not in ("Filled", "Cancelled", "ApiCancelled"):
        ib.cancelOrder(pos.stop_trade.order)
        ib.sleep(2)
        if pos.stop_trade.orderStatus.status == "Filled":  # stop filled while we were cancelling
            return stop_filled(journal, pos)
    price = _sell(ib, pos, pos.state.remaining)
    if price is None:
        # Put protection back so the shares aren't left naked; retried next tick.
        pos.stop_trade = ib.placeOrder(pos.contract, StopOrder("SELL", pos.state.remaining, pos.state.stop, tif="DAY"))
        journal.update_trend_join_position(pos.id, stop_order_id=pos.stop_trade.order.orderId)
        return False
    pos.realized_pnl += (price - pos.state.entry) * pos.state.remaining
    journal.log_order(pos.symbol, "SELL", pos.state.remaining, "MKT", broker="IBKR", status="filled")
    journal.close_trend_join_position(pos.id, reason, pos.realized_pnl)
    logger.info("%s: CLOSED %d @ %.2f (%s), trade P&L %.2f", pos.symbol, pos.state.remaining, price, reason, pos.realized_pnl)
    return True


def stop_filled(journal: Journal, pos: LivePosition) -> bool:
    price = pos.stop_trade.orderStatus.avgFillPrice or pos.state.stop
    pos.realized_pnl += (price - pos.state.entry) * pos.state.remaining
    reason = "trailing_stop" if pos.state.breakeven else "stop_loss"
    journal.close_trend_join_position(pos.id, reason, pos.realized_pnl)
    logger.info("%s: STOPPED OUT %d @ %.2f (%s), trade P&L %.2f", pos.symbol, pos.state.remaining, price, reason, pos.realized_pnl)
    return True


def manage(ib: IB, journal: Journal, pos: LivePosition, flatten_time: time) -> bool:
    """One monitoring tick. Returns True once the position is fully closed."""
    if pos.stop_trade is not None and pos.stop_trade.orderStatus.status == "Filled":
        return stop_filled(journal, pos)

    bm = settings.trend_join_bar_minutes
    now = pd.Timestamp(_now_et())
    rth = completed_bars(fetch_intraday_bars(ib, pos.symbol, bm, duration="1 D", use_rth=True), now, bm)
    rth = rth[rth.index.date == now.date()]
    try:
        _, _, price = get_quote(ib, pos.contract)
    except ValueError:
        price = float(rth["close"].iloc[-1])  # no live quote: fall back to the last completed bar

    swing = latest_swing_low(rth, settings.trend_join_swing_bars, settings.trend_join_swing_bars)
    plan = plan_exit(
        pos.state, price, now.time(), swing, flatten_time,
        settings.trend_join_partial_r, settings.trend_join_partial_fraction, settings.trend_join_breakeven_r,
    )
    if plan.flatten_reason:
        return flatten(ib, journal, pos, plan.flatten_reason)

    s = pos.state
    if plan.partial_triggered:
        s.partial_taken = True
        if plan.partial_qty > 0:
            # Shrink the stop first so stop + partial can never oversell.
            _modify_stop(ib, pos, s.stop, s.remaining - plan.partial_qty)
            fill = _sell(ib, pos, plan.partial_qty)
            if fill is None:
                _modify_stop(ib, pos, s.stop, s.remaining)
                s.partial_taken = False
            else:
                s.remaining -= plan.partial_qty
                pos.realized_pnl += (fill - s.entry) * plan.partial_qty
                journal.log_order(pos.symbol, "SELL", plan.partial_qty, "MKT", broker="IBKR", status="filled")
                logger.info("%s: +%.2fR partial, sold %d @ %.2f", pos.symbol, settings.trend_join_partial_r, plan.partial_qty, fill)
        journal.update_trend_join_position(
            pos.id, partial_taken=int(s.partial_taken), remaining=s.remaining, realized_pnl=pos.realized_pnl,
        )

    if plan.breakeven_reached:
        s.breakeven = True
    if plan.new_stop is not None:
        logger.info("%s: stop %.2f -> %.2f%s", pos.symbol, s.stop, plan.new_stop, " (breakeven/trail)" if s.breakeven else "")
        s.stop = plan.new_stop
        _modify_stop(ib, pos, s.stop, s.remaining)
    if plan.breakeven_reached or plan.new_stop is not None:
        journal.update_trend_join_position(pos.id, breakeven=int(s.breakeven), stop_price=s.stop)

    logger.info("%s: holding %d, price %.2f, entry %.2f, stop %.2f, %.2fR",
                pos.symbol, s.remaining, price, s.entry, s.stop, (price - s.entry) / s.r if s.r > 0 else 0.0)
    return False


def recover_positions(ib: IB, client: IBKRClient, journal: Journal) -> list[LivePosition]:
    held = {p.contract.symbol: int(p.position) for p in client.positions() if p.contract.secType == "STK"}
    ib.reqOpenOrders()  # this client's working orders (the stops placed before a restart)
    open_trades = {t.order.orderId: t for t in ib.openTrades()}
    recovered = []
    for row in journal.open_trend_join_positions():
        sym = row["symbol"]
        contract = Stock(sym, "SMART", "USD")
        ib.qualifyContracts(contract)
        state = PositionState(
            row["entry_price"], row["initial_stop"], row["stop_price"], row["qty"], row["remaining"],
            bool(row["partial_taken"]), bool(row["breakeven"]),
        )
        pos = LivePosition(row["id"], sym, contract, state, open_trades.get(row["stop_order_id"]),
                           row["realized_pnl"] or 0.0)
        shares = held.get(sym, 0)
        if shares <= 0:
            # Stop (or a manual close) filled while the bot was down.
            pnl = pos.realized_pnl + (state.stop - state.entry) * state.remaining
            journal.close_trend_join_position(pos.id, "closed_while_offline", pnl)
            logger.warning("%s: no longer held — marking closed (P&L estimated at stop: %.2f)", sym, pnl)
            continue
        state.remaining = min(state.remaining, shares)
        if pos.stop_trade is None:
            pos.stop_trade = ib.placeOrder(contract, StopOrder("SELL", state.remaining, state.stop, tif="DAY"))
            journal.update_trend_join_position(pos.id, stop_order_id=pos.stop_trade.order.orderId)
            logger.warning("%s: protective stop missing on recovery — re-placed @ %.2f", sym, state.stop)
        logger.warning("%s: recovered open position (%d @ %.2f, stop %.2f)", sym, state.remaining, state.entry, state.stop)
        recovered.append(pos)
    return recovered


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def main() -> None:
    journal = Journal(settings.db_path)
    entry_start = _hhmm(settings.trend_join_entry_start)
    entry_end = _hhmm(settings.trend_join_entry_end)
    flatten_time = _hhmm(settings.trend_join_flatten_time)
    prefilter_times = sorted(_hhmm(t) for t in settings.trend_join_prefilter_time_list)
    bm = settings.trend_join_bar_minutes

    with IBKRClient(settings, client_id=settings.trend_join_client_id) as client:
        ib = client.ib
        ib.reqMarketDataType(3)  # live where subscribed, delayed otherwise
        positions = recover_positions(ib, client, journal)
        candidates: list[GapCandidate] = []
        volume_history: dict[str, pd.DataFrame] = {}
        last_bar_bucket = None

        logger.info("Trend Join live loop starting (%d recovered positions)", len(positions))
        # Runs past flatten_time only while a flatten is still being retried,
        # and never past the close.
        while _now_et().time() < MARKET_CLOSE:
            now = _now_et()
            today = now.date().isoformat()

            due = [t for t in prefilter_times if t <= now.time()]
            if due and now.time() < entry_end:
                prefilter_times = [t for t in prefilter_times if t > now.time()]
                try:
                    candidates = run_prefilter(now.date(), excluded_symbols(client, journal, today))
                except Exception:
                    logger.exception("Prefilter failed — keeping previous candidate list")

            for pos in list(positions):
                try:
                    if manage(ib, journal, pos, flatten_time):
                        positions.remove(pos)
                except Exception:
                    logger.exception("%s: error managing position (will retry next tick)", pos.symbol)

            bucket = (now.hour * 60 + now.minute) // bm
            if entry_start <= now.time() <= entry_end and bucket != last_bar_bucket and candidates:
                last_bar_bucket = bucket
                traded = journal.trend_join_symbols_traded_on(today)
                excluded = excluded_symbols(client, journal, today)
                for cand in candidates:
                    if len(positions) >= settings.trend_join_max_positions:
                        break
                    if len(traded) >= settings.trend_join_max_trades_per_day:
                        logger.info("Max trades/day (%d) reached", settings.trend_join_max_trades_per_day)
                        break
                    if ib_symbol(cand.symbol) in excluded:
                        continue
                    try:
                        pos = try_enter(ib, client, journal, cand, volume_history, pd.Timestamp(now))
                    except Exception:
                        logger.exception("%s: entry check failed", cand.symbol)
                        continue
                    if pos is not None:
                        positions.append(pos)
                        traded.append(pos.symbol)
                        excluded.add(pos.symbol)

            if now.time() >= flatten_time and not positions:
                break
            ib.sleep(TICK_SECONDS)

        if positions:
            logger.error(
                "Still holding %s at the close — flatten failed; broker-side DAY stops expire at the close, "
                "so these need manual attention",
                [p.symbol for p in positions],
            )
        logger.info("Trend Join live loop finished for today")


if __name__ == "__main__":
    main()
