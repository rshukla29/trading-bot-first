"""2026-11-06: a condor stop closed 4 of 10 contracts, the journal still
said 10, and the next run would have sent a 10-lot close against 6 held."""
import sqlite3
from types import SimpleNamespace

import tradingbot.run_iron_condor_bot as bot
from tradingbot.journal import Journal

CONIDS = dict(call_short_conid=1, call_long_conid=2, put_short_conid=3, put_long_conid=4)


def _journal(tmp_path, contracts=10):
    j = Journal(str(tmp_path / "j.db"))
    pid = j.open_condor_position("SPY", "2026-10-12T19:47:52+00:00", "20261127", 815.0, 820.0, 751.0, 746.0,
                                 *CONIDS.values(), credit_received=1.28, contracts=contracts)
    return j, pid


def _setup(monkeypatch, held, fills):
    """`held`: contracts IBKR shows per leg; `fills`: contracts filled by
    each close order as read right after it."""
    monkeypatch.setattr(bot, "_rehydrate_option", lambda ib, conid: conid)
    monkeypatch.setattr(bot, "current_cost", lambda ib, legs: 2.70)        # >= 2 x 1.28: stop_loss
    monkeypatch.setattr(bot, "condor_close_ladder", lambda ib, legs: [2.55, 2.91, 3.15])
    monkeypatch.setattr(bot, "wait_for_fill", lambda ib, t, timeout=60: False)
    monkeypatch.setattr(bot, "held_qty", lambda ib, conid: held)
    sent, trades = [], []

    def close(ib, symbol, legs, n, limit):
        got = fills[len(trades)]
        t = SimpleNamespace(order=SimpleNamespace(totalQuantity=n),
                            orderStatus=SimpleNamespace(filled=got, avgFillPrice=-limit if got else 0.0))
        sent.append(n)
        trades.append(t)
        return t, limit
    monkeypatch.setattr(bot, "close_condor", close)
    ib = SimpleNamespace(cancelOrder=lambda o: None, sleep=lambda s: None)
    return ib, sent, trades


def _rows(j):
    with sqlite3.connect(j.db_path) as c:
        c.row_factory = sqlite3.Row
        return c.execute("SELECT * FROM condor_positions ORDER BY id").fetchall()


def test_partial_close_books_the_filled_part_and_keeps_the_rest_open(tmp_path, monkeypatch):
    j, pid = _journal(tmp_path)
    ib, sent, trades = _setup(monkeypatch, held=10, fills=[0, 0, 3])

    def ladder_with_late_fill(ib_, legs):
        if len(trades) == 1:                     # attempt 1's contract filled after its cancel
            trades[0].orderStatus.filled, trades[0].orderStatus.avgFillPrice = 1, -2.55
        return [2.55, 2.91, 3.15]
    monkeypatch.setattr(bot, "condor_close_ladder", ladder_with_late_fill)
    bot.manage_position(ib, j, "SPY", j.open_condor_positions("SPY")[0])
    assert sent == [10, 9, 9]                    # the late fill is counted before re-sending
    rs = _rows(j)
    assert [(r["status"], r["contracts"]) for r in rs] == [("open", 6), ("closed", 4)]
    cost = (2.55 + 3 * 3.15) / 4
    assert rs[1]["exit_reason"] == "stop_loss" and abs(rs[1]["exit_cost"] - cost) < 1e-9
    assert abs(rs[1]["realized_pnl"] - (1.28 - cost) * 4 * 100) < 1e-6
    assert rs[1]["call_short_conid"] == 1 and rs[1]["entry_date"] == rs[0]["entry_date"]


def test_close_never_sends_more_than_ibkr_holds(tmp_path, monkeypatch):
    j, pid = _journal(tmp_path)                  # journal 10, IBKR 6
    ib, sent, _ = _setup(monkeypatch, held=-6, fills=[6])
    bot.manage_position(ib, j, "SPY", j.open_condor_positions("SPY")[0])
    assert sent == [6]
    assert j.open_condor_positions("SPY") == []


def test_condor_already_flat_at_ibkr_is_marked_closed(tmp_path, monkeypatch):
    j, pid = _journal(tmp_path)
    ib, sent, _ = _setup(monkeypatch, held=0, fills=[])
    bot.manage_position(ib, j, "SPY", j.open_condor_positions("SPY")[0])
    assert sent == []
    assert j.open_condor_positions("SPY") == []
