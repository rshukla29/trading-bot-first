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
from tradingbot.options.orb_strategy import OpeningRangeBreakoutStrategy, fib_extension_level, fib_extension_reached
from tradingbot.options.pricing import implied_vol, vix_calibrated_vol
from tradingbot.position_guard import (
    buy_back,
    clamp_sell_qty,
    held_qty,
    intended_short_conids,
    unexpected_shorts,
)
from tradingbot.risk import ladder_stop, profit_lock_floor, step_trailing_stop
from tradingbot.strategy.trend_join import completed_bars

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("ib_async").setLevel(logging.WARNING)  # its own wrapper logs every tick/portfolio update at INFO
logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
# Exit checks every 5s (was 60s until 2026-10-13: the 777P's step-up lock
# was $0.99 but the option fell from $1.13 to $0.91 between two 60s checks).
# Quotes are streamed (see _quote) so a check doesn't wait 3s per quote, and
# 5-min bars are fetched once per completed bar (see tick_symbol) to stay
# well inside IBKR's historical-data pacing (~60 requests / 10 min).
TICK_SECONDS = 5.0
HOLDING_LOG_EVERY = 60.0  # seconds between "holding" log lines
BAR_FETCH_DELAY = 5.0     # seconds after a 5-min boundary before fetching the completed bar

_streams: dict[int, object] = {}


def _quote(ib, contract) -> tuple[float, float, float]:
    """(bid, ask, mid) from a streaming market-data subscription, opened on
    first use and kept until stop_streams(). Falls back to a one-off
    get_quote while the stream has no valid bid/ask yet."""
    con_id = getattr(contract, "conId", None)
    if not (con_id and hasattr(ib, "reqMktData")):
        return get_quote(ib, contract)
    t = _streams.get(con_id)
    if t is None:
        t = _streams[con_id] = ib.reqMktData(contract, genericTickList="", snapshot=False)
    # Wait on the stream itself (up to ~3s on first use). Never fall back to
    # get_quote here: it subscribes and then cancels the same contract, which
    # cancels this stream too and leaves it frozen (found 2026-10-13).
    for _ in range(7):
        bid, ask = t.bid, t.ask
        if bid is not None and ask is not None and bid == bid and ask == ask and bid > 0 and ask > 0:
            return bid, ask, (bid + ask) / 2
        ib.sleep(0.5)
    raise ValueError(f"No valid streaming quote for {getattr(contract, 'localSymbol', '') or contract}: "
                     f"bid={t.bid} ask={t.ask}")


def stop_streams(ib, *contracts) -> None:
    for c in contracts:
        t = _streams.pop(getattr(c, "conId", None), None)
        if t is not None:
            ib.cancelMktData(c)


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
        "partial_pnl": row["realized_pnl"] or 0.0,
        "range_low": row["range_low"], "range_high": row["range_high"],
        # Persisted peak: the step-up stop survives a restart.
        "peak_premium": row["peak_premium"] or row["entry_premium"],
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
    _, _, spot_mid = _quote(ib, underlying)
    _, _, premium_mid = _quote(ib, position["contract"])

    # Step stop is based on the highest premium seen, so a spike to +21%
    # that fades still exits at +20%. Peak is in-memory only: after a
    # restart it rebuilds from the current premium.
    prev_peak = position.get("peak_premium", position["entry_premium"])
    position["peak_premium"] = max(prev_peak, premium_mid)
    if position["peak_premium"] > prev_peak:
        journal.update_orb_peak(position["id"], position["peak_premium"])
    step_stop = None
    if settings.options_step_stop_enabled:
        step_stop = step_trailing_stop(
            position["entry_premium"], position["peak_premium"], settings.step_stop_pct, settings.step_stop_buffer_pct,
        )

    lock_floor = profit_lock_floor(
        position["entry_premium"], position["peak_premium"],
        settings.options_lock_trigger_pct, settings.options_lock_keep_pct,
    )
    ladder = (
        ladder_stop(position["entry_premium"], position["peak_premium"], settings.options_ladder_trigger_pct,
                    settings.options_ladder_step_pct, settings.options_ladder_trail_pct)
        if settings.options_ladder_trigger_pct > 0 else None
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
        lock_floor_premium=lock_floor,
        ladder_stop_premium=ladder,
        fib_target_hit=(
            settings.options_fib_target_ext > 0
            and fib_extension_reached(position["right"], spot_mid, position["range_low"], position["range_high"],
                                      settings.options_fib_target_ext)
        ),
    )
    if reason is None:
        now_s = _now_et().timestamp()
        if now_s - position.get("last_hold_log", 0.0) < HOLDING_LOG_EVERY:
            return False
        position["last_hold_log"] = now_s
        logger.info(
            "%s: ORB position holding (premium %.2f vs entry %.2f, peak %.2f, locked-in stop %s, spot %.2f, "
            "last 5-min close %s vs VWAP %s)",
            symbol, premium_mid, position["entry_premium"], position["peak_premium"],
            (f"{ladder:.2f}" if ladder is not None else
             f"{lock_floor:.2f}" if lock_floor is not None else "not yet"), spot_mid,
            f"{bar_close:.2f}" if bar_close is not None else "n/a", f"{vwap:.2f}" if vwap is not None else "n/a",
        )
        return False

    # Never sell more than IBKR shows as held (see position_guard.py).
    qty = clamp_sell_qty(position["contracts"], held_qty(ib, position["contract"].conId))
    partial_pnl = position.get("partial_pnl", 0.0)
    if qty == 0:
        journal.close_orb_position(
            position["id"], datetime.now(timezone.utc).isoformat(), 0.0, "already_flat", partial_pnl,
        )
        logger.warning("%s: ORB position already flat at IBKR — marking closed", symbol)
        return True

    retry = position.get("exit_attempts", 0) > 0
    trade, _ = exit_orb_trade(ib, position["contract"], qty, at_bid=retry)
    if not wait_for_fill(ib, trade):
        ib.cancelOrder(trade.order)
        ib.sleep(2)  # let a fill that raced the cancel arrive before reading it
        done = int(trade.orderStatus.filled)
        position["exit_attempts"] = position.get("exit_attempts", 0) + 1
        if done:
            # Partly filled: book it and only try to sell the rest next tick.
            position["partial_pnl"] = partial_pnl + (
                (trade.orderStatus.avgFillPrice - position["entry_premium"]) * done * CONTRACT_MULTIPLIER
            )
            position["contracts"] -= done
            journal.record_orb_partial_exit(position["id"], position["contracts"], position["partial_pnl"])
        logger.warning(
            "%s: ORB close filled %d of %d before cancel — %d left, retrying at the bid next tick",
            symbol, done, qty, position["contracts"],
        )
        if position["contracts"] <= 0:
            journal.close_orb_position(
                position["id"], datetime.now(timezone.utc).isoformat(), trade.orderStatus.avgFillPrice,
                reason, position["partial_pnl"],
            )
            return True
        return False

    actual_exit = trade.orderStatus.avgFillPrice
    realized_pnl = partial_pnl + (actual_exit - position["entry_premium"]) * qty * CONTRACT_MULTIPLIER
    journal.close_orb_position(
        position["id"], datetime.now(timezone.utc).isoformat(), actual_exit, reason, realized_pnl,
    )
    journal.log_order(symbol, "CLOSE_ORB", qty, "OPT", broker="IBKR", status="filled")
    logger.info(
        "%s: CLOSED ORB position (%s), actual exit premium=%.2f, pnl=%.2f",
        symbol, reason, actual_exit, realized_pnl,
    )
    return True


def check_entry(
    ib, journal: Journal, symbol: str, strategy: OpeningRangeBreakoutStrategy, chain, equity: float,
    bars: pd.DataFrame, entries_today: dict | None = None,
) -> dict | None:
    """`bars` must be completed bars only (see tick_symbol). Enters on the
    day's first breakout per direction, or on a re-entry when the trend
    resumes after an earlier trade that direction has closed."""
    signal = strategy.generate_signal(symbol, bars)
    journal.log_signal(signal.symbol, signal.action, signal.underlying_price, signal.reason, source="run_orb_bot")

    right = None
    if settings.options_confirm_bars > 1:
        right = strategy.confirmed_breakout(bars, settings.options_confirm_bars, entries_today or {})
        if right is not None:
            signal = type(signal)(
                symbol, "BUY_CALL" if right == "C" else "BUY_PUT", signal.underlying_price, signal.timestamp,
                f"{settings.options_confirm_bars} closes {'above' if right == 'C' else 'below'} the opening range",
            )
    elif signal.action in ("BUY_CALL", "BUY_PUT"):
        right = "C" if signal.action == "BUY_CALL" else "P"
    if right is not None:
        today_bars = bars[bars.index.date == bars.index[-1].date()]
        vwap = float(session_vwap(today_bars).iloc[-1]) if settings.options_require_vwap_side else None
        if not strategy.passes_entry_filters(bars, right, settings.options_min_range_pct, vwap):
            logger.info("%s: %s breakout filtered out (range < %.1f%% of the open, or wrong side of VWAP %s)",
                        symbol, "call" if right == "C" else "put", settings.options_min_range_pct * 100,
                        f"{vwap:.2f}" if vwap is not None else "n/a")
            return None
    else:
        if settings.options_reentry_mode == "vwap":
            today = bars[bars.index.date == bars.index[-1].date()]
            right = strategy.vwap_reentry_direction(
                today, float(session_vwap(today).iloc[-1]), entries_today or {}, settings.options_max_reentries,
            )
        else:
            right = strategy.reentry_direction(bars, entries_today or {}, settings.options_max_reentries)
        if right is None:
            logger.info("%s: %s (%s)", symbol, signal.action, signal.reason)
            return None
        signal = type(signal)(
            symbol, "BUY_CALL" if right == "C" else "BUY_PUT", signal.underlying_price, signal.timestamp,
            ("re-entry: VWAP pullback held" if settings.options_reentry_mode == "vwap"
             else f"re-entry: trend resumed to a new session {'high' if right == 'C' else 'low'}"),
        )
        journal.log_signal(symbol, signal.action, signal.underlying_price, signal.reason, source="run_orb_bot")
    day_range = strategy.range_for_day(bars, bars.index[-1].date())
    if day_range is None:
        logger.warning("%s: breakout signal fired but opening range unavailable, skipping", symbol)
        return None
    range_low, range_high = day_range

    contract = select_orb_contract(
        ib, symbol, chain, signal.underlying_price, right,
        settings.options_otm_pct, settings.options_strike_increment, settings.options_dte_days,
    )
    if held_qty(ib, contract.conId) != 0:
        # IBKR nets positions per contract: buying one another strategy is
        # short (e.g. a credit spread's short leg) would mix the two.
        logger.warning("%s: %s already has a position (another strategy) — skipping", symbol, contract.localSymbol)
        return None
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
        ib.cancelOrder(trade.order)
        ib.sleep(2)
        done = int(trade.orderStatus.filled)
        if not done:
            logger.warning("%s: ORB open order did not fill (status=%s) — cancelled", symbol, trade.orderStatus.status)
            return None
        # Partly filled: those contracts are a real position — track them
        # (untracked, they'd be left unmanaged until the expiry guard).
        logger.warning("%s: ORB open order filled %d of %d before cancel — managing %d", symbol, done, contracts, done)
        contracts = done

    actual_entry = trade.orderStatus.avgFillPrice
    position_id = journal.open_orb_position(
        symbol, datetime.now(timezone.utc).isoformat(), contract.lastTradeDateOrContractMonth,
        contract.strike, right, contract.conId, actual_entry, contracts, range_low, range_high,
    )
    journal.log_order(symbol, "OPEN_ORB", contracts, "OPT", broker="IBKR", status="filled")
    record_entry_iv(ib, journal, position_id, actual_entry, signal.underlying_price, contract.strike, right)
    logger.info(
        "%s: OPENED ORB %s %s%s x%d, actual entry premium=%.2f (%s)",
        symbol, contract.lastTradeDateOrContractMonth, contract.strike, right, contracts, actual_entry, signal.reason,
    )
    return {
        "id": position_id, "contract": contract, "right": right, "entry_premium": actual_entry,
        "contracts": contracts, "range_low": range_low, "range_high": range_high,
    }


def _last_entry_time() -> time:
    h, m = settings.options_last_entry_time.split(":")
    return time(int(h), int(m))


def iv_pause_active(journal: Journal) -> bool:
    """True when the median fill-IV / VIX-model-IV ratio over the last
    orb_iv_check_trades entries (at least 5) exceeds orb_iv_pause_ratio."""
    if journal is None:
        return False
    ratios = journal.recent_orb_iv_ratios(settings.orb_iv_check_trades)
    return len(ratios) >= 5 and float(pd.Series(ratios).median()) > settings.orb_iv_pause_ratio


def latest_vix(ib) -> float | None:
    """Last completed 5-min VIX close (IBKR historical data, which this
    account has for the VIX index)."""
    from ib_async import Index
    try:
        bars = fetch_intraday_bars_contract(ib, Index("VIX", "CBOE"))
        return float(bars["close"].iloc[-1]) if not bars.empty else None
    except Exception:
        logger.exception("Could not fetch VIX")
        return None


def fetch_intraday_bars_contract(ib, contract) -> pd.DataFrame:
    ib.qualifyContracts(contract)
    from ib_async import util
    df = util.df(ib.reqHistoricalData(contract, "", "1 D", "5 mins", "TRADES", True, formatDate=2))
    return df if df is not None else pd.DataFrame()


def record_entry_iv(ib, journal: Journal, position_id: int, fill: float, spot: float, strike: float, right: str) -> None:
    """Store the fill's implied vol next to the VIX-calibrated estimate, so
    drift between real option prices and the backtest's pricing is
    visible (and can pause entries — see iv_pause_active)."""
    vix = latest_vix(ib)
    if vix is None:
        return
    now = _now_et()
    close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    minutes_left = max(1.0, (close - now).total_seconds() / 60)
    t_years = minutes_left / (365.25 * 24 * 60)
    fill_iv = implied_vol(fill, spot, strike, t_years, settings.options_rate, right)
    model_iv = vix_calibrated_vol(vix, minutes_left)
    journal.set_orb_entry_iv(position_id, fill_iv, model_iv)
    logger.info("Fill implied vol %.1f%% vs VIX-model %.1f%% (VIX %.2f) — ratio %.2f",
                fill_iv * 100, model_iv * 100, vix, fill_iv / model_iv)


def repair_short_options(ib, journal: Journal, symbols: list[str]) -> None:
    """ORB only ever buys options. Any short option on an ORB symbol that
    isn't an iron condor leg is a mistake — buy it back immediately."""
    allowed = intended_short_conids(journal)
    for pos in unexpected_shorts(ib.positions(), allowed):
        if pos.contract.secType == "OPT" and pos.contract.symbol in symbols:
            trade = buy_back(ib, pos)
            if wait_for_fill(ib, trade, timeout=60):
                logger.error("%s: unintended short bought back @ %.2f",
                             pos.contract.localSymbol, trade.orderStatus.avgFillPrice)
                journal.log_order(pos.contract.symbol, "BUY_TO_CLOSE_SHORT", int(abs(pos.position)), "OPT",
                                  broker="IBKR", status="filled")


def tick_symbol(ib, client, journal: Journal, strategy: OpeningRangeBreakoutStrategy, state: dict) -> None:
    """One monitoring tick for one symbol: manage its open position, or
    look for an entry on each newly-completed bar."""
    symbol = state["symbol"]
    if state["position"] is not None:
        if check_exit(ib, journal, symbol, state["underlying"], state["position"]):
            if hasattr(ib, "cancelMktData"):
                stop_streams(ib, state["position"]["contract"])
            state["position"] = None
        return
    # Entries are judged on completed 5-min bars, so fetch bars only once per
    # bar — a few seconds after each 5-min boundary — not every 5s tick.
    now_ts = pd.Timestamp(_now_et())
    boundary = now_ts.floor(f"{settings.options_bar_minutes}min")
    if state.get("fetched_boundary") == boundary or (now_ts - boundary).total_seconds() < BAR_FETCH_DELAY:
        return
    state["fetched_boundary"] = boundary
    # Completed bars only. IBKR returns the still-forming bar last; judging
    # it seconds after it opened (fix 2026-09-28) meant its "close" was
    # still inside the range, and by the next check the real breakout bar
    # counted as "already broken" — live took 4 of 16 backtest signals
    # 9/11-9/25. Now each bar is judged once, right after it completes,
    # exactly like the backtest.
    bm = settings.options_bar_minutes
    bars = completed_bars(fetch_intraday_bars(ib, symbol, bm), pd.Timestamp(_now_et()), bm)
    if bars.empty:
        return
    latest_bar_ts = bars.index[-1]
    if latest_bar_ts != state["last_bar_ts"]:
        state["last_bar_ts"] = latest_bar_ts
        if _now_et().time() >= _last_entry_time():
            return  # no new entries after the cutoff; open positions are still managed above
        if iv_pause_active(journal):
            logger.warning("%s: ORB entries paused — recent fills priced >%.2fx the VIX-calibrated estimate",
                           symbol, settings.orb_iv_pause_ratio)
            return
        state["position"] = check_entry(
            ib, journal, symbol, strategy, state["chain"], client.net_liquidation(), bars, state["entries_today"],
        )
        if state["position"] is not None:
            state["entries_today"][state["position"]["right"]] += 1


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
                    # From the journal so a mid-day restart keeps the re-entry count.
                    "entries_today": journal.orb_entries_on(symbol, datetime.now(timezone.utc).date().isoformat()),
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
            try:
                repair_short_options(ib, journal, symbols)
            except Exception:
                logger.exception("Short-position check failed")
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
