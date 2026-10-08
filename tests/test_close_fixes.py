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
