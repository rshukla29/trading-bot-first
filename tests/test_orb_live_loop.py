"""Regression tests for run_orb_bot's entry loop (tick_symbol).

The 2026-09-28 bug: IBKR returns the still-forming bar last, and the loop
judged it seconds after it opened — its "close" was still inside the
range, and on the next check the real breakout bar counted as "already
broken", so the entry was skipped (live took 4 of 16 backtest signals).
These tests replay what IBKR returns at each tick and assert the loop
judges only completed bars and catches a mid-bar breakout.
"""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import tradingbot.run_orb_bot as orb
from tradingbot.options.orb_strategy import OpeningRangeBreakoutStrategy

ET = ZoneInfo("America/New_York")
DAY = "2026-09-22"

# Opening range 9:30-9:45: high 100.5, low 99.5. Quiet bars at 9:45 and
# 9:50, then the 9:55 bar breaks out and closes at 102 (above the range).
COMPLETED = [
    ("09:30", 100.0, 100.5, 99.5, 100.0),
    ("09:35", 100.0, 100.4, 99.6, 100.1),
    ("09:40", 100.1, 100.3, 99.7, 100.0),
    ("09:45", 100.0, 100.3, 99.8, 100.1),
    ("09:50", 100.1, 100.4, 99.9, 100.2),
    ("09:55", 100.2, 102.1, 100.1, 102.0),  # breakout happens mid-bar
]


def _bars(rows):
    idx = pd.DatetimeIndex([pd.Timestamp(f"{DAY} {t}", tz=ET) for t, *_ in rows])
    return pd.DataFrame(
        [r[1:] + (1_000_000,) for r in rows], index=idx, columns=["open", "high", "low", "close", "volume"],
    )


def ibkr_response_at(now: str) -> pd.DataFrame:
    """What reqHistoricalData returns at `now`: every bar that has started,
    the last one still forming (seconds old, so its close ~ its open)."""
    now_ts = pd.Timestamp(f"{DAY} {now}", tz=ET)
    rows = [r for r in COMPLETED if pd.Timestamp(f"{DAY} {r[0]}", tz=ET) + pd.Timedelta(minutes=5) <= now_ts]
    forming_start = now_ts.floor("5min")
    last_close = rows[-1][4]
    rows.append((forming_start.strftime("%H:%M"), last_close, last_close, last_close, last_close))
    return _bars(rows)


@pytest.fixture
def harness(monkeypatch):
    strategy = OpeningRangeBreakoutStrategy(opening_range_minutes=15, bar_minutes=5)
    calls = []  # (tick time, last bar judged, signal action)
    clock = {"now": None}

    monkeypatch.setattr(orb, "_now_et", lambda: datetime.fromisoformat(f"{DAY}T{clock['now']}").replace(tzinfo=ET))
    monkeypatch.setattr(orb, "fetch_intraday_bars", lambda ib, symbol, bm, **kw: ibkr_response_at(clock["now"]))

    def fake_check_entry(ib, journal, symbol, strat, chain, equity, bars, entries_today=None):
        calls.append((clock["now"], bars.index[-1].strftime("%H:%M"), strat.generate_signal(symbol, bars).action))
        return None  # no order placed in tests

    monkeypatch.setattr(orb, "check_entry", fake_check_entry)
    state = {"symbol": "SPY", "underlying": None, "chain": None, "position": None, "last_bar_ts": None,
             "entries_today": {"C": 0, "P": 0}}
    client = SimpleNamespace(net_liquidation=lambda: 100_000.0)

    def tick(now: str):
        clock["now"] = now
        orb.tick_symbol(None, client, None, strategy, state)

    return tick, calls


def test_mid_bar_breakout_is_caught_on_the_completed_bar(harness):
    tick, calls = harness
    # The exact tick times from the 2026-09-22 log that missed the breakout.
    for now in ("09:46:04", "09:51:06", "09:55:07", "10:00:09"):
        tick(now)
    assert calls[-1] == ("10:00:09", "09:55", "BUY_CALL")


def test_forming_bar_is_never_judged(harness):
    tick, calls = harness
    tick("09:55:07")  # the 9:55 bar is 7 seconds old
    assert calls and calls[-1][1] == "09:50"


def test_each_completed_bar_is_judged_once(harness):
    tick, calls = harness
    for now in ("09:51:00", "09:52:00", "09:53:00", "09:54:00"):
        tick(now)
    assert [c[1] for c in calls] == ["09:45"]



# --- re-entry rule (shared by backtest and live) -------------------------------

def test_reentry_after_a_closed_call_on_new_session_high():
    strategy = OpeningRangeBreakoutStrategy(opening_range_minutes=15, bar_minutes=5)
    bars = _bars(COMPLETED + [("10:00", 102.0, 103.0, 101.5, 102.9)])  # new high of day above range
    assert strategy.reentry_direction(bars, {"C": 1, "P": 0}, 2) == "C"
    assert strategy.reentry_direction(bars, {"C": 0, "P": 0}, 2) is None   # no earlier call: that's a first entry
    assert strategy.reentry_direction(bars, {"C": 3, "P": 0}, 2) is None   # used up (1 + 2 re-entries)
    assert strategy.reentry_direction(bars, {"C": 1, "P": 0}, 0) is None   # disabled


def test_no_reentry_below_the_session_high():
    strategy = OpeningRangeBreakoutStrategy(opening_range_minutes=15, bar_minutes=5)
    bars = _bars(COMPLETED + [("10:00", 101.5, 101.9, 101.0, 101.8)])  # 101.8 < 9:55 high 102.1
    assert strategy.reentry_direction(bars, {"C": 1, "P": 0}, 2) is None


def test_reentry_ignores_previous_days_bars():
    strategy = OpeningRangeBreakoutStrategy(opening_range_minutes=15, bar_minutes=5)
    yesterday = _bars([("15:55", 150.0, 150.0, 150.0, 150.0)])
    yesterday.index = yesterday.index - pd.Timedelta(days=1)
    bars = pd.concat([yesterday, _bars(COMPLETED + [("10:00", 102.0, 103.0, 101.5, 102.9)])])
    assert strategy.reentry_direction(bars, {"C": 1, "P": 0}, 2) == "C"
