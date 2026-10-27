from types import SimpleNamespace

from tradingbot.ibkr.orders import place_bracket_buy, place_market_sell
from tradingbot.protection import check_protection, working_stop_qty_by_symbol
from tradingbot.run_snapshot import _render_stop_coverage_section


def _trade(symbol, action="SELL", order_type="STP", qty=10, aux=90.0, sec_type="STK"):
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol, secType=sec_type),
        order=SimpleNamespace(action=action, orderType=order_type, totalQuantity=qty, auxPrice=aux),
    )


class FakeIB:
    """Just enough of ib_async.IB to capture what the order helpers send."""

    def __init__(self, open_trades=(), held=None):
        self.placed = []
        self.cancelled = []
        self._open = list(open_trades)
        self._held = held or {}  # symbol -> shares
        self.client = SimpleNamespace(getReqId=iter(range(1000, 2000)).__next__)
        self.ack_status = "PreSubmitted"

    def positions(self):
        return [SimpleNamespace(contract=SimpleNamespace(symbol=s, secType="STK", conId=0), position=q)
                for s, q in self._held.items()]

    def qualifyContracts(self, *contracts):
        return list(contracts)

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return SimpleNamespace(contract=contract, order=order, orderStatus=SimpleNamespace(status=self.ack_status))

    def reqOpenOrders(self):
        return self._open

    def cancelOrder(self, order):
        self.cancelled.append(order)

    def sleep(self, _):
        pass


# --- the 2026-09-25 regression: stops must outlive the entry day ------------

def test_bracket_stop_is_gtc_not_day():
    ib = FakeIB()
    place_bracket_buy(ib, "AAA", 10, 100.0, 0.10)
    (_, parent), (_, stop) = ib.placed
    assert stop.orderType == "STP" and stop.tif == "GTC"
    assert stop.auxPrice == 90.0
    assert parent.tif == "DAY"


def test_market_sell_cancels_stop_first_and_sets_tif():
    stop = _trade("AAA")
    other = _trade("BBB")
    ib = FakeIB(open_trades=[stop, other], held={"AAA": 10})
    place_market_sell(ib, "AAA", 10)
    assert ib.cancelled == [stop.order]  # only AAA's stop
    (_, sell), = ib.placed
    assert sell.action == "SELL" and sell.tif == "DAY"


# --- audit decision ----------------------------------------------------------

def test_covered_position_is_ok():
    assert check_protection(100, 95, 10, 10, 0.10).action == "ok"


def test_uncovered_above_stop_places_stop():
    check = check_protection(100, 95, 10, 0, 0.10)
    assert check.action == "place_stop" and check.stop_price == 90.0


def test_uncovered_below_stop_sells():
    # CBIO: cost 19.00, stop 17.10, trading 14.07 with no stop.
    assert check_protection(19.0, 14.07, 116, 0, 0.10).action == "sell_now"


def test_partial_cover_counts_as_unprotected():
    assert check_protection(100, 95, 10, 4, 0.10).action == "place_stop"


def test_working_stop_qty_ignores_non_stops_and_options():
    trades = [
        _trade("AAA", qty=6), _trade("AAA", qty=4),
        _trade("AAA", order_type="LMT"), _trade("AAA", action="BUY"),
        _trade("SPY", sec_type="OPT"),
    ]
    assert working_stop_qty_by_symbol(trades) == {"AAA": 10}


# --- snapshot visibility -----------------------------------------------------

def _pos(symbol, qty):
    return SimpleNamespace(contract=SimpleNamespace(symbol=symbol, secType="STK"), position=qty)


def test_snapshot_flags_unprotected_bot_positions_only():
    html = "\n".join(_render_stop_coverage_section(
        [_pos("BAC", 33), _pos("CBIO", 116), _pos("ASTX", 1000)],
        [_trade("BAC", qty=33, aux=53.52)],
        {"BAC": "rsi2", "CBIO": "rsi2"},
    ))
    assert "WARNING" in html and "CBIO" in html.split("WARNING")[1].split("</p>")[0]
    assert "ASTX" not in html.split("WARNING")[1].split("</p>")[0]
    assert "manual — not managed by bots" in html
    assert "53.52" in html


# --- step stop raise -----------------------------------------------------------

from tradingbot.protection import plan_stop_raise  # noqa: E402


def test_plan_stop_raise_moves_up_in_steps():
    # cost $10, initial stop $9: +11% -> $11, +21% -> $12
    assert plan_stop_raise(10.0, 11.10, 9.00, 0.10, 0.01) == 11.00
    assert plan_stop_raise(10.0, 12.10, 11.00, 0.10, 0.01) == 12.00


def test_plan_stop_raise_never_lowers_or_repeats():
    assert plan_stop_raise(10.0, 11.50, 11.00, 0.10, 0.01) is None  # already at the +10% step
    assert plan_stop_raise(10.0, 11.10, 12.00, 0.10, 0.01) is None  # price fell back: stop stays at 12
    assert plan_stop_raise(10.0, 10.50, 9.00, 0.10, 0.01) is None   # below +11%: keep -10%


# --- orphan stops & pending exits (2026-09-28 BAC incident) ------------------

from tradingbot.protection import orphan_stops, symbols_with_pending_exit  # noqa: E402


def test_stop_on_flat_position_is_an_orphan():
    bac_stop = _trade("BAC", qty=33, aux=53.34)
    xlf_stop = _trade("XLF", qty=36, aux=49.32)
    assert orphan_stops([bac_stop, xlf_stop], held_symbols={"XLF"}) == [bac_stop]


def test_non_stop_orders_are_never_orphans():
    assert orphan_stops([_trade("BAC", order_type="MKT"), _trade("BAC", action="BUY")], set()) == []


def test_pending_market_sell_marks_exit_in_progress():
    trades = [_trade("BAC", order_type="MKT"), _trade("XLF")]  # XLF only has its stop
    assert symbols_with_pending_exit(trades) == {"BAC"}


def test_snapshot_warns_about_orphan_stop():
    html = "\n".join(_render_stop_coverage_section(
        [_pos("XLF", 36)], [_trade("XLF", qty=36, aux=49.32), _trade("BAC", qty=33, aux=53.34)], {"XLF": "rsi2"},
    ))
    assert "no position behind them" in html and "BAC SELL 33 @ 53.34" in html


def test_stop_with_pending_bracket_buy_is_not_an_orphan():
    # RSI(2) buys after the close: parent BUY waits for the open, its stop
    # already exists. The MA bot running right after must not cancel it.
    buy = _trade("NEW", action="BUY", order_type="MKT")
    stop = _trade("NEW", qty=10, aux=45.0)
    assert orphan_stops([buy, stop], held_symbols=set()) == []


def test_market_sell_never_sells_more_than_held():
    ib = FakeIB(held={"AAA": 4})
    place_market_sell(ib, "AAA", 10)
    (_, sell), = ib.placed
    assert sell.totalQuantity == 4


def test_market_sell_skips_when_nothing_held():
    ib = FakeIB(held={})
    assert place_market_sell(ib, "AAA", 10) is None
    assert ib.placed == []



def test_bracket_waits_for_ibkr_acknowledgement_and_flags_a_lost_order():
    from tradingbot.ibkr.orders import await_ack
    ib = FakeIB()
    ib.ack_status = "PendingSubmit"   # IBKR never acknowledges (the 2026-10-08 INTC case)
    trades = place_bracket_buy(ib, "INTC", 18, 107.08, 0.10)
    assert await_ack(ib, trades, timeout=1.0) is False
    ib.ack_status = "PreSubmitted"
    assert await_ack(ib, place_bracket_buy(ib, "GRMN", 7, 268.44, 0.10), timeout=1.0) is True


def test_stop_lifted_to_max_loss_from_real_cost():
    # LLY 2026-10-20: cost 1265.93, stop 1116.31 (priced off the signal close) = -11.8%.
    assert plan_stop_raise(1265.93, 1250.0, 1116.31, 0.10, 0.01, max_loss_pct=0.10) == 1139.34


def test_stop_floor_never_loosens_or_fires_on_the_spot():
    # LRCX: stop 258.23 is tighter than cost x 0.9 (248.36) -> leave it.
    assert plan_stop_raise(275.96, 270.0, 258.23, 0.10, 0.01, max_loss_pct=0.10) is None
    # Price already at the floor: lifting the stop would sell immediately -> leave it.
    assert plan_stop_raise(1265.93, 1140.0, 1116.31, 0.10, 0.01, max_loss_pct=0.10) is None
    # The step rule still wins once it's higher.
    assert plan_stop_raise(10.0, 11.10, 9.00, 0.10, 0.01, max_loss_pct=0.10) == 11.00


# --- P&L by strategy (daily / weekly / monthly reports) ----------------------

def test_strategy_pnl_splits_by_strategy_and_never_double_counts(tmp_path):
    from datetime import date
    from tradingbot.journal import Journal
    from tradingbot.run_snapshot import _render_strategy_pnl_section
    from tradingbot.strategy_pnl import strategy_pnl
    db = str(tmp_path / "j.db")
    j = Journal(db)
    # ORB: exit 19:50 UTC on 10-20 = 10-20 ET
    pid = j.open_orb_position("SPY", "2026-10-20T14:00:00+00:00", "20261020", 768, "C", 1, 1.38, 10, 764, 766)
    j.close_orb_position(pid, "2026-10-20T14:02:25+00:00", 1.63, "fib_target", 250.0)
    # Trend Join HAS: its own journal; its IBKR fill must not count again
    tj = j.open_trend_join_position("HAS", "2026-10-20", 108.40, 18, 100.39, 1, "gap")
    j.close_trend_join_position(tj, "eod_flatten", 14.76)
    import sqlite3
    with sqlite3.connect(db) as c:   # close_* stamps "now"; pin it to the trade's day
        c.execute("UPDATE trend_join_positions SET exit_ts='2026-10-20T19:51:16+00:00' WHERE id=?", (tj,))
    j.record_closed_trade("e1", "2026-10-20", "HAS", "SLD", 18, 109.22, 12.72)
    # RSI(2) AMAT: bought by run_rsi2_bot, stopped out in two lots = one trade
    j.log_signal("AMAT", "BUY", 432.0, "RSI(2)=1", source="run_rsi2_bot")
    j.log_order("AMAT", "BUY", 4, "MKT+STP", broker="IBKR", status="submitted")
    with sqlite3.connect(db) as c:   # bought after the 10-16 close
        c.execute("UPDATE signals SET ts='2026-10-16T20:30:08+00:00' WHERE symbol='AMAT'")
        c.execute("UPDATE orders SET ts='2026-10-16T20:30:10+00:00' WHERE symbol='AMAT'")
    j.record_closed_trade("e2", "2026-10-20", "AMAT", "SLD", 3, 401.8, -90.0)
    j.record_closed_trade("e3", "2026-10-20", "AMAT", "SLD", 1, 401.8, -32.83)
    # A manual sale (no bot order)
    j.record_closed_trade("e4", "2026-10-20", "ASTX", "SLD", 100, 5.0, 47.07)

    df = strategy_pnl(db, date(2026, 10, 20), date(2026, 10, 20)).set_index("strategy")
    assert df.loc["ORB (SPY 0DTE options)", "realized"] == 250.0
    assert df.loc["Trend Join", "realized"] == 14.76                  # journal, not +12.72 again
    assert (df.loc["RSI(2)", "trades"], df.loc["RSI(2)", "realized"]) == (1, -122.83)
    assert df.loc["Manual / other", "realized"] == 47.07
    assert round(df["realized"].sum(), 2) == 189.0
    html = "\n".join(_render_strategy_pnl_section(date(2026, 10, 20), date(2026, 10, 20), "today"))
    assert "P&amp;L by strategy" in html   # renders (reads settings.db_path; content checked above)
