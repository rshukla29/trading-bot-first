"""2026-10-08 fixes: no-bid quotes, credit-spread close when the long leg is
worthless, condor price ladders."""
from types import SimpleNamespace

import tradingbot.run_credit_orb_bot as cob
from tradingbot.options.live_chain import no_bid_quote
from tradingbot.options.live_condor import debit_ladder


def test_no_bid_counts_as_zero_when_ask_is_valid():
    assert no_bid_quote(-1, 0.01) == (0.0, 0.01, 0.005)   # the 2026-10-08 779C quote
    assert no_bid_quote(None, 0.02) == (0.0, 0.02, 0.01)
    assert no_bid_quote(0.10, 0.12) == (0.10, 0.12, 0.11)
    assert no_bid_quote(0.10, -1) is None                  # no ask: genuinely no quote
    assert no_bid_quote(-1, float("nan")) is None


def test_debit_ladder_steps_up_from_mid_to_natural():
    assert debit_ladder(0.45, 0.65) == [0.45, 0.55, 0.65]
    assert debit_ladder(0.45, 0.45) == [0.45]              # tight market: one price
    assert debit_ladder(0.004, 0.01) == [0.01]             # never below a penny


class _J:
    def __init__(self):
        self.closed, self.updates, self.orders = [], [], []

    def close_credit_orb(self, pid, cost, reason, pnl):
        self.closed.append((reason, round(cost, 2), round(pnl, 2)))

    def update_credit_orb_contracts(self, pid, n):
        self.updates.append(n)

    def log_order(self, *a, **k):
        self.orders.append(a)


def test_credit_close_buys_back_only_the_short_leg_when_long_has_no_bid(monkeypatch):
    placed = []
    short, long = SimpleNamespace(conId=1, localSymbol="777C"), SimpleNamespace(conId=2, localSymbol="779C")
    monkeypatch.setattr(cob, "held_qty", lambda ib, con_id: -2 if con_id == 1 else 2)
    monkeypatch.setattr(cob, "get_quote_lenient", lambda ib, c: (0.01, 0.02, 0.015) if c is short else (0.0, 0.01, 0.005))

    def place(contract, order):
        placed.append((contract, order))
        return SimpleNamespace(order=order, orderStatus=SimpleNamespace(filled=2, avgFillPrice=0.02))

    monkeypatch.setattr(cob, "wait_for_fill", lambda ib, t, timeout=45: True)
    ib = SimpleNamespace(placeOrder=place, cancelOrder=lambda o: None, sleep=lambda s: None)
    j = _J()
    pos = {"id": 3, "short": short, "long": long, "contracts": 2, "credit": 0.11, "right": "C", "close_attempts": 0}
    assert cob.close_spread(ib, j, pos, "SPY", "close_1550") is True
    (contract, order), = placed
    assert contract is short and order.action == "BUY" and order.totalQuantity == 2   # single leg, not the combo
    assert j.closed == [("close_1550", 0.02, 18.0)]                                    # (0.11 - 0.02) * 2 * 100


def test_condor_open_ladder_adds_natural_only_in_a_tight_market():
    from tradingbot.options.live_condor import credit_ladder_for_open
    assert credit_ladder_for_open(1.25, 1.23) == [1.25, 1.24, 1.23]   # the 2026-10-09 market: natural within 10%
    assert credit_ladder_for_open(1.25, 0.90) == [1.25, 1.07]         # wide market: never give away 28%


def test_credit_close_counts_a_fill_that_lands_after_the_cancel(monkeypatch):
    # 2026-10-15: attempt 1 read "0 filled", cancelled, then 1 of 2 filled
    # anyway; attempt 2 closed the last one; the journal booked only 1.
    short, long = SimpleNamespace(conId=1, localSymbol="764P"), SimpleNamespace(conId=2, localSymbol="762P")
    held = {"short": -2}
    monkeypatch.setattr(cob, "held_qty", lambda ib, con_id: held["short"] if con_id == 1 else -held["short"])
    monkeypatch.setattr(cob, "get_quote_lenient", lambda ib, c: (0.01, 0.03, 0.02) if c is short else (0.01, 0.02, 0.015))
    monkeypatch.setattr(cob, "wait_for_fill", lambda ib, t, timeout=45: False)
    trades = []

    def place(contract, order):
        t = SimpleNamespace(order=order, orderStatus=SimpleNamespace(filled=0, avgFillPrice=0.0))
        if trades:                       # attempt 2 fills its 1 contract
            t.orderStatus.filled, t.orderStatus.avgFillPrice = order.totalQuantity, 0.01
            held["short"] += order.totalQuantity
        trades.append(t)
        return t

    ib = SimpleNamespace(placeOrder=place, cancelOrder=lambda o: None, sleep=lambda s: None)
    j = _J()
    pos = {"id": 6, "short": short, "long": long, "contracts": 2, "credit": 0.19, "right": "P", "close_attempts": 0}
    assert cob.close_spread(ib, j, pos, "SPY", "close_1550") is False     # reads 0 of 2
    trades[0].orderStatus.filled, trades[0].orderStatus.avgFillPrice = 1, 0.01   # late fill arrives
    held["short"] = -1
    assert cob.close_spread(ib, j, pos, "SPY", "close_1550") is True      # buys the last 1, then done
    assert trades[1].order.totalQuantity == 1
    assert j.closed == [("close_1550", 0.01, 36.0)]                       # both spreads: (0.19 - 0.01) * 2 * 100


def test_credit_close_already_flat_with_no_bot_fills(monkeypatch):
    monkeypatch.setattr(cob, "held_qty", lambda ib, con_id: 0)
    j = _J()
    pos = {"id": 7, "short": SimpleNamespace(conId=1), "long": SimpleNamespace(conId=2), "contracts": 2,
           "credit": 0.19, "right": "P", "close_attempts": 0}
    assert cob.close_spread(None, j, pos, "SPY", "close_1550") is True
    assert j.closed == [("already_flat", 0.0, 0.0)]


def test_get_quote_rejects_nan_so_callers_fall_back():
    # 2026-10-20: HAS quoted nan/nan; `nan <= 0` is False, so it passed as a price.
    import pytest
    from tradingbot.options.live_chain import get_quote
    ticker = SimpleNamespace(bid=float("nan"), ask=float("nan"))
    ib = SimpleNamespace(reqMktData=lambda c, genericTickList="", snapshot=False: ticker,
                         sleep=lambda s: None, cancelMktData=lambda c: None)
    with pytest.raises(ValueError):
        get_quote(ib, SimpleNamespace(localSymbol="HAS"))
    ticker.bid, ticker.ask = 109.20, 109.24
    assert get_quote(ib, SimpleNamespace(localSymbol="HAS"))[2] == pytest.approx(109.22)
