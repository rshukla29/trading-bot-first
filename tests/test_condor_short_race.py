"""2026-10-19: ORB's short-option guard bought back 4 of the iron condor's
10 short 802 calls while the condor order was still filling — the condor's
journal row was only written after the fill."""
from types import SimpleNamespace

import tradingbot.run_orb_bot as orb
from tradingbot.journal import Journal
from tradingbot.position_guard import intended_short_conids


def _open_pending(journal):
    return journal.open_condor_position(
        "SPY", "2026-10-19T19:45:48+00:00", "20261218", 802, 807, 744, 739,
        927884414, 927884510, 927885528, 927885431, 1.99, 10, status="pending",
    )


def test_pending_condor_legs_are_intended_before_the_fill(tmp_path):
    journal = Journal(str(tmp_path / "j.db"))
    pid = _open_pending(journal)
    assert {927884414, 927885528} <= intended_short_conids(journal)    # protected before any fill
    assert journal.open_condor_positions("SPY") == []                  # not managed until confirmed
    journal.confirm_condor(pid, "2026-10-19T19:48:00+00:00", 1.94, 10)
    assert {927884414, 927885528} <= intended_short_conids(journal)
    (row,) = journal.open_condor_positions("SPY")
    assert row["credit_received"] == 1.94 and row["contracts"] == 10


def test_unfilled_condor_is_cancelled_and_does_not_block_the_next_entry(tmp_path):
    journal = Journal(str(tmp_path / "j.db"))
    journal.cancel_condor(_open_pending(journal), "not_filled")
    assert 927884414 not in intended_short_conids(journal)
    assert journal.last_condor_entry_date("SPY") is None


def test_guard_waits_before_buying_back_an_unknown_short(tmp_path, monkeypatch):
    journal = Journal(str(tmp_path / "j.db"))
    t = {"now": 1_760_900_000.0}
    monkeypatch.setattr(orb, "_now_et", lambda: SimpleNamespace(timestamp=lambda: t["now"]))
    monkeypatch.setattr(orb, "_short_first_seen", {})
    bought = []
    monkeypatch.setattr(orb, "buy_back", lambda ib, pos: bought.append(pos.contract.conId) or SimpleNamespace())
    monkeypatch.setattr(orb, "wait_for_fill", lambda ib, trade, timeout=60: False)
    short = SimpleNamespace(position=-4, contract=SimpleNamespace(conId=555, secType="OPT", symbol="SPY",
                                                                  localSymbol="SPY 802C"))
    held = [short]
    ib = SimpleNamespace(positions=lambda: held)

    orb.repair_short_options(ib, journal, ["SPY"])          # first seen: wait
    t["now"] += 30
    orb.repair_short_options(ib, journal, ["SPY"])          # still inside the grace period
    assert bought == []
    t["now"] += 31
    orb.repair_short_options(ib, journal, ["SPY"])          # 61s and still unowned: buy back
    assert bought == [555]


def test_guard_forgets_a_short_that_turns_out_to_be_owned(tmp_path, monkeypatch):
    journal = Journal(str(tmp_path / "j.db"))
    t = {"now": 1_760_900_000.0}
    monkeypatch.setattr(orb, "_now_et", lambda: SimpleNamespace(timestamp=lambda: t["now"]))
    monkeypatch.setattr(orb, "_short_first_seen", {})
    bought = []
    monkeypatch.setattr(orb, "buy_back", lambda ib, pos: bought.append(pos.contract.conId) or SimpleNamespace())
    short = SimpleNamespace(position=-4, contract=SimpleNamespace(conId=927884414, secType="OPT", symbol="SPY",
                                                                  localSymbol="SPY 802C"))
    ib = SimpleNamespace(positions=lambda: [short])

    orb.repair_short_options(ib, journal, ["SPY"])          # seen before the row exists
    _open_pending(journal)                                   # the condor bot records it
    t["now"] += 120
    orb.repair_short_options(ib, journal, ["SPY"])
    assert bought == []
