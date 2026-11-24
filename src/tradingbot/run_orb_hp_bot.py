"""Long-running intraday entrypoint for ORB-0DTE-HP: SPY same-day (0DTE)
calls and puts when completed 5-minute candles line up with the 9 EMA, the
50 and 200 SMA, the session VWAP and a fresh MACD cross. Rules:
options/orb_hp_strategy.py.

Each 5-minute candle is judged once, a few seconds after it completes:
  - CALL signal (close above the 9 EMA, 50 SMA, 200 SMA and VWAP; the MACD
    line crosses above its 9-EMA signal line on that candle; MACD above 0):
    buy a call at once, strike nearest SPY;
  - PUT signal: the mirror image; buy a put.
One trade a day: a call OR a put, whichever signal comes first between
9:35 and 11:00am (ORB_HP_FIRST_ENTRY_TIME / ORB_HP_LAST_ENTRY_TIME; the
Rules tab's trades per day counts calls and puts together), held to 3:55pm.
Never a call and a put at the same time: a signal is only taken while
nothing is open. ORB_HP_EXIT=candle instead sells a call after the first
red candle and a put after the first green one. The Rules tab's stop loss, step stop and
profit target apply when switched on (off by default). ORB_HP_SIDES picks
calls, puts or both (the default). Never buys a contract another bot
already holds.

The 200 SMA needs 200 completed 5-min candles (about 2.5 sessions): 5 days
of bars are fetched. A restart mid-day recovers open positions from the
journal (orb_hp_positions). Refuses a non-paper account unless
LIVE_TRADING=true.

    python -m tradingbot.run_orb_hp_bot
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timezone

import pandas as pd
from ib_async import Stock

from tradingbot import control, rules
from tradingbot.config import settings
from tradingbot.ibkr.client import IBKRClient
from tradingbot.ibkr.data import fetch_intraday_bars
from tradingbot.journal import Journal
from tradingbot.options.live_chain import get_quote, get_real_chain
from tradingbot.options.live_orb import (
    CONTRACT_MULTIPLIER,
    EOD_FLATTEN_TIME,
    enter_orb_trade,
    exit_orb_trade,
    select_orb_contract,
    wait_for_fill,
)
from tradingbot.options.orb_hp_strategy import CALL, PUT, HpParams, candle_exit, exit_reason, latest_signal
from tradingbot.position_guard import clamp_sell_qty, held_qty
from tradingbot.run_orb_bot import ET, _now_et, _price, _quote, _rehydrate_option, stop_streams
from tradingbot.strategy.trend_join import completed_bars

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("ib_async").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

SID = "orb_hp"
TABLE = "orb_hp_positions"
SOURCE = "run_orb_hp_bot"
MARKET_OPEN, MARKET_CLOSE = time(9, 30), time(16, 0)
TICK_SECONDS = 5.0
BAR_FETCH_DELAY = 5.0      # seconds after a candle boundary before fetching the completed candle
HISTORY = "5 D"            # >= 200 completed 5-min candles (78 per session) on any morning
HOLDING_LOG_EVERY = 60.0
NAMES = {CALL: "call", PUT: "put"}

GATE = control.Gate(SID, logger)


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def in_entry_window(now: time) -> bool:
    return _hhmm(settings.orb_hp_first_entry_time) <= now < _hhmm(settings.orb_hp_last_entry_time)


def sides() -> str:
    s = settings.orb_hp_sides.upper()
    return "".join(x for x in "CP" if x in s) or "CP"


def recover_open_positions(ib, journal: Journal, symbol: str) -> dict[str, dict | None]:
    """{"C": position or None, "P": ...} from the journal (a restart mid-day)."""
    out: dict[str, dict | None] = {CALL: None, PUT: None}
    for row in journal.open_option_rows(TABLE, symbol):
        if row["expiry"] < _now_et().strftime("%Y%m%d"):
            pnl = (row["realized_pnl"] or 0.0) - row["entry_premium"] * row["contracts"] * CONTRACT_MULTIPLIER
            journal.close_orb_position(row["id"], datetime.now(timezone.utc).isoformat(), 0.0, "expired", pnl,
                                       table=TABLE)
            logger.warning("%s: ORB-0DTE-HP %s%s %s expired unsold — booked at $0 (pnl %.2f)",
                           symbol, row["strike"], row["right"], row["expiry"], pnl)
            continue
        contract = _rehydrate_option(ib, row["conid"])
        logger.warning("Recovered open ORB-0DTE-HP position %s%s x%d (entry %.2f) — resuming monitoring",
                       row["strike"], row["right"], row["contracts"], row["entry_premium"])
        out[row["right"]] = {"id": row["id"], "contract": contract, "right": row["right"],
                             "entry_premium": row["entry_premium"], "contracts": row["contracts"],
                             "partial_pnl": row["realized_pnl"] or 0.0,
                             "peak_premium": row["peak_premium"] or row["entry_premium"]}
    return out


def rules_allow_entry(state: dict, right: str, equity: float) -> bool:
    """Nothing open, the Rules tab's max trades per day (calls and puts
    together) and its daily loss limit."""
    if not flat(state):
        return False
    r = rules.load(SID)
    if sum(state["entries_today"].values()) >= r["trades"]:
        if not state.get("trades_logged"):
            logger.info("%s: max trades per day (%d) reached — no more ORB-0DTE-HP entries today",
                        state["symbol"], r["trades"])
            state["trades_logged"] = True
        return False
    hit = rules.daily_limit_hit(r, r.capital(equity))
    if hit is not None:
        if not state.get("daily_logged"):
            logger.warning("%s: daily loss limit ($%.0f) reached — no more ORB-0DTE-HP entries today",
                           state["symbol"], hit)
            state["daily_logged"] = True
        return False
    return True


def enter(ib, journal: Journal, state: dict, pend: dict, spot: float, equity: float) -> dict | None:
    """Buy the call or put for a pending signal. Returns the new position or None."""
    symbol, right = state["symbol"], pend["right"]
    contract = select_orb_contract(ib, symbol, state["chain"], spot, right, settings.orb_hp_otm_pct,
                                   settings.options_strike_increment, 0)
    if held_qty(ib, contract.conId) != 0:
        logger.warning("%s already has a position (another bot) — skipping this ORB-0DTE-HP signal",
                       contract.localSymbol)
        return None
    _, _, mid = get_quote(ib, contract)
    if mid <= 0.01:
        logger.warning("%s quoted at %.3f — too small, skipping", contract.localSymbol, mid)
        return None
    r = rules.load(SID)
    capital = r.capital(equity)
    cost = mid * CONTRACT_MULTIPLIER
    max_prem = r.max_premium(capital)
    if max_prem is not None and cost > max_prem:
        logger.info("%s costs $%.0f a contract, above the max premium of $%.0f — skipping",
                    contract.localSymbol, cost, max_prem)
        return None
    contracts = min(r.quantity(capital, cost), r["maxQty"])
    if contracts <= 0:
        logger.info("Computed 0 contracts (capital $%.0f, contract $%.0f) — skipping", capital, cost)
        return None

    trade, _ = enter_orb_trade(ib, contract, contracts)
    if not wait_for_fill(ib, trade):
        ib.cancelOrder(trade.order)
        ib.sleep(2)
        done = int(trade.orderStatus.filled)
        if not done:
            logger.warning("ORB-0DTE-HP buy did not fill (status=%s) — cancelled", trade.orderStatus.status)
            return None
        logger.warning("ORB-0DTE-HP buy filled %d of %d before cancel — managing %d", done, contracts, done)
        contracts = done
    fill = trade.orderStatus.avgFillPrice
    pid = journal.open_orb_position(symbol, datetime.now(timezone.utc).isoformat(), contract.lastTradeDateOrContractMonth,
                                    contract.strike, right, contract.conId, fill, contracts,
                                    pend["sma50"], pend["sma200"], table=TABLE)
    journal.log_order(symbol, "OPEN_ORB_HP", contracts, "OPT", broker="IBKR", status="filled")
    logger.info("OPENED ORB-0DTE-HP %s x%d at %.2f (%s %.2f)", contract.localSymbol, contracts, fill, symbol, spot)
    return {"id": pid, "contract": contract, "right": right, "entry_premium": fill, "contracts": contracts,
            "partial_pnl": 0.0, "peak_premium": fill}


def check_exit(ib, journal: Journal, state: dict, pos: dict, force_reason: str | None = None) -> bool:
    """Sell one position if a rule says so. True once it's fully sold."""
    premium = _price(ib, pos["contract"])
    if premium > pos["peak_premium"]:
        pos["peak_premium"] = premium
        journal.update_orb_peak(pos["id"], premium, table=TABLE)
    r = rules.load(SID)
    ladder = r.ladder_stop(pos["entry_premium"], pos["peak_premium"], CONTRACT_MULTIPLIER)
    reason = force_reason or exit_reason(
        _now_et().time(), premium, pos["entry_premium"], eod=EOD_FLATTEN_TIME,
        stop_fraction=r.stop_fraction(pos["entry_premium"], CONTRACT_MULTIPLIER), ladder_stop=ladder,
        target_fraction=r.profit_target(), candle=bool(pos.get("candle_exit")))
    if reason is None:
        now_s = _now_et().timestamp()
        if now_s - pos.get("last_hold_log", 0.0) >= HOLDING_LOG_EVERY:
            pos["last_hold_log"] = now_s
            logger.info("ORB-0DTE-HP holding %s (premium %.2f vs entry %.2f, peak %.2f)",
                        pos["contract"].localSymbol, premium, pos["entry_premium"], pos["peak_premium"])
        return False

    qty = clamp_sell_qty(pos["contracts"], held_qty(ib, pos["contract"].conId))
    partial = pos.get("partial_pnl", 0.0)
    now_iso = datetime.now(timezone.utc).isoformat()
    if qty == 0:
        journal.close_orb_position(pos["id"], now_iso, 0.0, "already_flat", partial, table=TABLE)
        logger.warning("ORB-0DTE-HP %s already flat at IBKR — marking closed", pos["contract"].localSymbol)
        return True
    trade, _ = exit_orb_trade(ib, pos["contract"], qty, at_bid=pos.get("exit_attempts", 0) > 0, quote=_quote)
    if not wait_for_fill(ib, trade):
        ib.cancelOrder(trade.order)
        ib.sleep(2)
        done = int(trade.orderStatus.filled)
        pos["exit_attempts"] = pos.get("exit_attempts", 0) + 1
        if done:
            pos["partial_pnl"] = partial + (trade.orderStatus.avgFillPrice - pos["entry_premium"]) * done * CONTRACT_MULTIPLIER
            pos["contracts"] -= done
            journal.record_orb_partial_exit(pos["id"], pos["contracts"], pos["partial_pnl"], table=TABLE)
        logger.warning("ORB-0DTE-HP sell filled %d of %d before cancel — %d left, retrying at the bid",
                       done, qty, pos["contracts"])
        if pos["contracts"] <= 0:
            journal.close_orb_position(pos["id"], now_iso, trade.orderStatus.avgFillPrice, reason,
                                       pos["partial_pnl"], table=TABLE)
            return True
        return False
    fill = trade.orderStatus.avgFillPrice
    pnl = partial + (fill - pos["entry_premium"]) * qty * CONTRACT_MULTIPLIER
    journal.close_orb_position(pos["id"], now_iso, fill, reason, pnl, table=TABLE)
    journal.log_order(state["symbol"], "CLOSE_ORB_HP", qty, "OPT", broker="IBKR", status="filled")
    logger.info("CLOSED ORB-0DTE-HP %s (%s) at %.2f, pnl %.2f", pos["contract"].localSymbol, reason, fill, pnl)
    return True


def on_new_bar(journal: Journal, state: dict, bars: pd.DataFrame) -> None:
    """Judge the candle that just completed: (candle exit mode) flag open
    positions to sell, and arm a signal only while nothing is open (or the
    open position is being sold)."""
    sig, row = latest_signal(bars, HpParams())
    bar_ts = bars.index[-1]
    last = bars.iloc[-1]
    for right, pend in list(state["pending"].items()):
        if pend is not None and bar_ts > pend["bar_ts"]:
            logger.info("%s: ORB-0DTE-HP %s signal from %s lapsed (not entered within one candle)",
                        state["symbol"], NAMES[right], pend["bar_ts"].strftime("%H:%M"))
            state["pending"][right] = None
    if settings.orb_hp_exit == "candle":
        for pos in state["positions"].values():
            if pos is not None and candle_exit(pos["right"], float(last["open"]), float(last["close"])):
                pos["candle_exit"] = True
    if sig is None:
        return
    reason = (f"close {row['close']:.2f} {'above' if sig == CALL else 'below'} 9 EMA {row['ema9']:.2f}, "
              f"50 SMA {row['sma50']:.2f}, 200 SMA {row['sma200']:.2f}, VWAP {row['vwap']:.2f}; "
              f"MACD {row['macd']:.3f} crossed {'above' if sig == CALL else 'below'} signal {row['signal']:.3f}")
    action = "BUY_CALL" if sig == CALL else "BUY_PUT"
    journal.log_signal(state["symbol"], action, float(row["close"]), reason, source=SOURCE)
    logger.info("%s: ORB-0DTE-HP %s signal on the %s candle: %s", state["symbol"], action,
                bar_ts.strftime("%H:%M"), reason)
    if sig in sides() and all(p is None or p.get("candle_exit") for p in state["positions"].values()):
        state["pending"][sig] = {"bar_ts": bar_ts, "right": sig, "sma50": float(row["sma50"]),
                                 "sma200": float(row["sma200"])}


def tick(ib, client, journal: Journal, state: dict) -> None:
    now = _now_et()
    now_ts = pd.Timestamp(now)
    bm = settings.orb_hp_bar_minutes
    boundary = now_ts.floor(f"{bm}min")
    if state.get("fetched_boundary") != boundary and (now_ts - boundary).total_seconds() >= BAR_FETCH_DELAY:
        state["fetched_boundary"] = boundary
        bars = completed_bars(fetch_intraday_bars(ib, state["symbol"], bm, duration=HISTORY), now_ts, bm)
        if not bars.empty and bars.index[-1] != state.get("last_bar_ts"):
            state["last_bar_ts"] = bars.index[-1]
            on_new_bar(journal, state, bars)

    force = "manual_stop" if GATE.wants_close() else None
    for right in (CALL, PUT):
        pos = state["positions"][right]
        if pos is not None and check_exit(ib, journal, state, pos, force_reason=force):
            stop_streams(ib, pos["contract"])
            state["positions"][right] = None

    for right in (CALL, PUT):
        pend = state["pending"][right]
        if pend is None or not flat(state):
            continue
        state["pending"][right] = None
        if not GATE.entries_allowed() or not in_entry_window(now.time()):
            continue
        equity = client.net_liquidation()
        if rules_allow_entry(state, right, equity):
            pos = enter(ib, journal, state, pend, _price(ib, state["underlying"]), equity)
            if pos is not None:
                state["positions"][right] = pos
                state["entries_today"][right] += 1
                state["pending"] = {CALL: None, PUT: None}   # one trade: drop the other side's signal
                return


def flat(state: dict) -> bool:
    return all(p is None for p in state["positions"].values())


def new_state(ib, journal: Journal, symbol: str, today: str) -> dict:
    """One symbol's own candles, signal, position and trade count."""
    underlying = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(underlying)
    return {
        "symbol": symbol, "underlying": underlying, "chain": get_real_chain(ib, symbol),
        "positions": recover_open_positions(ib, journal, symbol), "pending": {CALL: None, PUT: None},
        "entries_today": {r: journal.option_entries_on(TABLE, symbol, today, right=r) for r in (CALL, PUT)},
    }


def main() -> None:
    symbols = settings.orb_hp_symbol_list
    journal = Journal(settings.db_path)
    with IBKRClient(settings, client_id=settings.orb_hp_client_id) as client:
        ib = client.ib
        today = datetime.now(timezone.utc).date().isoformat()
        states = []
        for symbol in symbols:
            try:
                states.append(new_state(ib, journal, symbol, today))
            except Exception:
                logger.exception("%s: couldn't set up ORB-0DTE-HP — skipping it today", symbol)
        if not states:
            logger.error("ORB-0DTE-HP: no symbol could be set up — not running today")
            return
        if GATE.stopped() and all(flat(s) for s in states):
            logger.warning("ORB-0DTE-HP is stopped from the dashboard and has no open position — not running today")
            GATE.close_done()
            return
        now = _now_et()
        if now.time() < MARKET_OPEN:
            wait_s = (datetime.combine(now.date(), MARKET_OPEN, tzinfo=ET) - now).total_seconds()
            logger.info("Started before market open, waiting %.0fs for 9:30am ET", wait_s)
            ib.sleep(max(0.0, wait_s))

        logger.info("ORB-0DTE-HP live loop starting for %s (%d-min candles, sides %s, exit %s)",
                    ", ".join(s["symbol"] for s in states), settings.orb_hp_bar_minutes, sides(),
                    settings.orb_hp_exit)
        while _now_et().time() < MARKET_CLOSE:
            for state in states:
                try:
                    tick(ib, client, journal, state)
                except Exception:
                    logger.exception("%s: ORB-0DTE-HP tick failed — will retry next tick", state["symbol"])
            if GATE.stopped() and all(flat(s) for s in states):
                GATE.close_done()
                logger.warning("ORB-0DTE-HP stopped from the dashboard and flat — exiting")
                break
            ib.sleep(TICK_SECONDS)

        for state in states:
            for right, pos in state["positions"].items():
                if pos is not None:
                    logger.warning("%s: ORB-0DTE-HP %s still open at market close, final safety-net sell",
                                   state["symbol"], NAMES[right])
                    try:
                        check_exit(ib, journal, state, pos, force_reason="eod_flatten")
                    except Exception:
                        logger.exception("%s: ORB-0DTE-HP final sell failed", state["symbol"])
        logger.info("ORB-0DTE-HP live loop finished for today")


if __name__ == "__main__":
    main()
