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

    def __init__(self, open_trades=()):
        self.placed = []
        self.cancelled = []
        self._open = list(open_trades)
        self.client = SimpleNamespace(getReqId=iter(range(1000, 2000)).__next__)

    def qualifyContracts(self, *contracts):
        return list(contracts)

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return SimpleNamespace(contract=contract, order=order)

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
    ib = FakeIB(open_trades=[stop, other])
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
