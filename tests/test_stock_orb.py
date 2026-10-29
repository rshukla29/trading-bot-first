"""Strategy #7, Stock ORB — the day-trader-skill pack's rules."""
from datetime import time
from types import SimpleNamespace

import pandas as pd
import pytest

from tradingbot.journal import Journal
from tradingbot.strategy.stock_orb import (
    find_trigger,
    opening_range,
    plan_trade,
    share_quantity,
    simulate_exit,
)

DAY = "2026-10-27"


def _bars(rows, day=DAY):
    idx = pd.DatetimeIndex([pd.Timestamp(f"{day} {t}", tz="America/New_York") for t, *_ in rows])
    return pd.DataFrame([r[1:] + (10_000,) for r in rows], index=idx,
                        columns=["open", "high", "low", "close", "volume"])


RANGE = [("09:30", 100.0, 101.0, 99.0, 100.5), ("09:35", 100.5, 101.5, 100.0, 101.0),
         ("09:40", 101.0, 101.2, 100.2, 100.8)]          # high 101.5, low 99.0


def test_opening_range_needs_every_bar():
    assert opening_range(_bars(RANGE)) == (101.5, 99.0)
    assert opening_range(_bars(RANGE[:2] + [("09:45", 1, 1, 1, 1)])) is None


def test_a_wick_above_the_high_is_not_a_trigger():
    day = _bars(RANGE + [("09:45", 101.0, 102.0, 100.9, 101.4),   # wick to 102, close 101.4 < 101.5
                         ("09:50", 101.4, 101.9, 101.3, 101.8)])  # close above -> trigger
    t = find_trigger(day)
    assert t.ts.strftime("%H:%M") == "09:50" and t.entry == 101.8 and (t.or_high, t.or_low) == (101.5, 99.0)


def test_trigger_must_end_inside_the_entry_window():
    day = _bars(RANGE + [("09:45", 101.0, 102.0, 100.9, 101.8)])
    assert find_trigger(day, first_entry=time(10, 0)) is None
    assert find_trigger(day).ts.strftime("%H:%M") == "09:45"   # ends 9:50, inside 9:45-15:30


def test_stop_at_range_midpoint_target_at_r_multiple():
    p = plan_trade(101.8, 101.5, 99.0, 2.0)
    assert p.stop == 100.25 and p.risk == pytest.approx(1.55) and p.target == pytest.approx(104.9)
    assert plan_trade(100.26, 101.5, 99.0, 2.0) is None   # risk 0.01 = 0.01% < 0.05%: inside the spread


def test_size_by_max_loss_capped_by_order_value():
    assert share_quantity(101.8, 100.25, 50, 4000) == 32       # 50 / 1.55
    assert share_quantity(364.18, 358.32, 50, 4000) == 8      # the pack's README TSLA example
    assert share_quantity(400.0, 399.0, 50, 4000) == 10       # $4,000 cap beats 50 shares


def test_stop_checked_before_target_and_flat_by():
    p = plan_trade(101.8, 101.5, 99.0, 1.0)
    both = _bars([("10:00", 101.8, 104.0, 100.0, 103.0)])
    assert simulate_exit(both, p)[0] == "stop"                 # touches both: count the loss
    up = _bars([("10:00", 101.8, 103.5, 101.5, 103.0)])
    assert simulate_exit(up, p)[:2] == ("target", p.target)
    late = _bars([("15:50", 102.0, 102.5, 101.9, 102.2), ("15:55", 102.2, 102.3, 102.0, 102.1)])
    assert simulate_exit(late, p)[:2] == ("flat", 102.2)       # out at the bar ending 15:55


def test_time_machine_is_the_packs_own_and_matches_its_reel():
    """Our adapter feeds the vendored Time Machine; on the pack's own TSLA
    fixture it must print the reel's exact result (SKILL.md)."""
    import gzip
    from pathlib import Path
    from tradingbot.strategy.stock_orb import Trigger, run_time_machine, time_machine_plan, time_machine_summary
    from tradingbot.vendor import timemachine as tm
    text = gzip.open(Path(__file__).parent / "fixtures/trader-timemachine/TSLA-60d5m.json.gz", "rt",
                     encoding="utf-8").read()
    trig = Trigger(pd.Timestamp("2026-10-19 10:10", tz="America/New_York"), 364.18, 362.18, 354.47, 0.0)
    p = plan_trade(trig.entry, trig.or_high, trig.or_low, 1.0)
    assert (p.stop, p.target) == (358.32, 370.04)          # the reel's stop and 1R target
    raw = time_machine_plan("TSLA", trig, p)
    assert raw["entry_time"] == "10:15 AM"
    doc = run_time_machine(raw, chart_text=text, now_utc=tm.parse_now("2026-10-19 10:50"), until=None)
    assert doc["verdict"] == "Close to a coin flip: target first 29.4 % vs stop first 25.5 %."
    assert (doc["history"]["triggered"], doc["history"]["target"], doc["history"]["stop"]) == (33, 13, 15)
    assert "seed 19" in time_machine_summary(doc)


def test_time_machine_plan_times_before_10am():
    from tradingbot.strategy.stock_orb import Trigger, time_machine_plan
    trig = Trigger(pd.Timestamp(f"{DAY} 09:45", tz="America/New_York"), 101.8, 101.5, 99.0, 0.0)
    raw = time_machine_plan("BRK-B", trig, plan_trade(101.8, 101.5, 99.0, 2.0))
    assert raw["entry_time"] == "9:50 AM" and raw["symbol"] == "BRK-B"   # not "09:50" (a bare 9 is fine, 1-8 means pm)


def test_journal_rows_budget_and_time_machine(tmp_path):
    from tradingbot.run_stock_orb_bot import risk_used_today
    j = Journal(str(tmp_path / "j.db"))
    pid = j.open_stock_orb_position("AAPL", DAY, "09:50", 101.5, 99.0, 101.8, 32, 100.25, 104.9, 11, 12)
    assert risk_used_today(j, DAY) == pytest.approx(32 * 1.55)
    j.set_stock_orb_time_machine(pid, "TM", -0.1, 7, 0.2)
    j.close_stock_orb_position(pid, 104.9, "target", 99.2)
    (row,) = j.stock_orb_rows_on(DAY)
    assert row["status"] == "closed" and row["tm_hist_trades"] == 7
    assert j.open_stock_orb_positions() == []


def test_live_bot_acts_only_on_a_fresh_trigger():
    from tradingbot.run_stock_orb_bot import fresh_trigger
    day = _bars(RANGE + [("09:45", 101.0, 102.0, 100.9, 101.8), ("09:50", 101.8, 102.1, 101.7, 102.0)])
    assert fresh_trigger(day.iloc[:4], pd.Timestamp(f"{DAY} 09:45", tz="America/New_York")) is not None
    assert fresh_trigger(day, pd.Timestamp(f"{DAY} 09:50", tz="America/New_York")) is None   # stale: don't chase


def test_bracket_exits_are_one_cancels_other():
    from tradingbot.run_stock_orb_bot import place_bracket
    placed = []
    ids = iter(range(100, 200))
    ib = SimpleNamespace(client=SimpleNamespace(getReqId=lambda: next(ids)),
                         placeOrder=lambda c, o: placed.append(o) or SimpleNamespace(order=o))
    place_bracket(ib, SimpleNamespace(), 32, 100.25, 104.9)
    parent, take, stop = placed
    assert parent.action == "BUY" and not parent.transmit and stop.transmit
    assert take.lmtPrice == 104.9 and stop.auxPrice == 100.25
    assert take.parentId == stop.parentId == parent.orderId and take.ocaGroup == stop.ocaGroup
