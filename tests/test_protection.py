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
