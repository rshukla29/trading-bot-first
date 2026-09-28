"""No unintended short positions — incl. a replay of the 2026-09-28 bug."""
from types import SimpleNamespace

import tradingbot.run_orb_bot as orb
from tradingbot.position_guard import clamp_sell_qty, unexpected_shorts


def _pos(con_id, qty, sec="OPT", symbol="IWM"):
    return SimpleNamespace(contract=SimpleNamespace(conId=con_id, secType=sec, symbol=symbol, localSymbol=symbol),
                           position=qty)


def test_clamp_sell_qty():
    assert clamp_sell_qty(10, 10) == 10
    assert clamp_sell_qty(10, 7) == 7     # partly sold already
    assert clamp_sell_qty(10, 0) == 0     # flat: sell nothing
    assert clamp_sell_qty(10, -2) == 0    # already short: never add to it


def test_unexpected_shorts_ignores_condor_legs():
    condor_call, condor_put = _pos(1, -1, symbol="SPY"), _pos(2, -1, symbol="SPY")
    stray = _pos(3, -11)
    long_ = _pos(4, 10)
    assert unexpected_shorts([condor_call, condor_put, stray, long_], allowed_conids={1, 2}) == [stray]


class _Journal:
    def __init__(self):
        self.closed, self.partials, self.orders = [], [], []

    def close_orb_position(self, pid, ts, exit_premium, reason, pnl):
        self.closed.append((reason, round(pnl, 2)))

    def record_orb_partial_exit(self, pid, remaining, pnl):
        self.partials.append((remaining, round(pnl, 2)))

    def log_order(self, *a, **k):
        self.orders.append(a)


def test_partial_fill_then_retry_never_oversells(monkeypatch):
    """Replays the IWM put: 10 held; first close fills 3 then times out;
    before the fix the retry sold 10 more. Now it sells only the 7 left."""
    held = {"qty": 10}
    sells = []
    fills = iter([3, 7])  # attempt 1 fills 3 of 10; attempt 2 fills all 7

    def fake_exit(ib, contract, qty, at_bid=False):
        n = next(fills)
        sells.append((qty, n, at_bid))
        held["qty"] -= n
        return SimpleNamespace(order=None, orderStatus=SimpleNamespace(filled=n, avgFillPrice=0.10)), 0.10

    monkeypatch.setattr(orb, "get_quote", lambda ib, c: (0.09, 0.11, 0.10))
    monkeypatch.setattr(orb, "latest_bar_vs_vwap", lambda ib, s: (100.0, 101.0))  # put, close < VWAP... call exits
    monkeypatch.setattr(orb, "exit_orb_trade", fake_exit)
    monkeypatch.setattr(orb, "held_qty", lambda ib, con_id: held["qty"])
    monkeypatch.setattr(orb, "wait_for_fill", lambda ib, trade: trade.orderStatus.filled == sells[-1][0])
    ib = SimpleNamespace(cancelOrder=lambda o: None, sleep=lambda s: None)
    journal = _Journal()
    position = {"id": 1, "contract": SimpleNamespace(conId=99), "right": "C", "entry_premium": 0.55,
                "contracts": 10, "range_low": 99.0, "range_high": 100.0}

    assert orb.check_exit(ib, journal, "IWM", None, position) is False   # 3 of 10 filled
    assert journal.partials == [(7, -135.0)]                              # (0.10-0.55)*3*100
    assert orb.check_exit(ib, journal, "IWM", None, position) is True    # remaining 7 sold, at the bid
    assert [s[0] for s in sells] == [10, 7] and sells[1][2] is True
    assert held["qty"] == 0                                               # flat, not short
    assert journal.closed == [("vwap_cross", -450.0)]                     # (0.10-0.55)*10*100


def test_exit_when_already_flat_sells_nothing(monkeypatch):
    monkeypatch.setattr(orb, "get_quote", lambda ib, c: (0.09, 0.11, 0.10))
    monkeypatch.setattr(orb, "latest_bar_vs_vwap", lambda ib, s: (100.0, 101.0))
    monkeypatch.setattr(orb, "held_qty", lambda ib, con_id: 0)
    monkeypatch.setattr(orb, "exit_orb_trade", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sold")))
    journal = _Journal()
    position = {"id": 1, "contract": SimpleNamespace(conId=99), "right": "C", "entry_premium": 0.55,
                "contracts": 10, "range_low": 99.0, "range_high": 100.0}
    assert orb.check_exit(SimpleNamespace(), journal, "IWM", None, position) is True
    assert journal.closed == [("already_flat", 0.0)]
