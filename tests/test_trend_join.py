from datetime import date, time

import numpy as np
import pandas as pd
import pytest

from tradingbot.strategy.trend_join import (
    GapCandidate,
    PositionState,
    check_entry,
    completed_bars,
    daily_prefilter,
    initial_stop,
    latest_swing_low,
    plan_exit,
    rank_candidates,
    relative_volume,
    share_quantity,
    split_session,
)

TODAY = date(2026, 9, 25)
ET = "America/New_York"


def _daily(closes, today_open, today=TODAY):
    idx = pd.bdate_range(end=pd.Timestamp(today), periods=len(closes) + 1)
    closes = list(closes) + [today_open]
    df = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1000}, index=idx)
    df.iloc[-1, df.columns.get_loc("open")] = today_open
    return df


# --- daily prefilter (D2, D3) ----------------------------------------------

def test_prefilter_passes_gap_above_uptrend():
    closes = np.linspace(50, 100, 220)  # uptrend: last close far above the 200-SMA
    cand = daily_prefilter("AAA", _daily(closes, 104.0), TODAY, min_gap_pct=3.0, min_price=3.0)
    assert cand is not None
    assert cand.gap_pct == 4.0
    assert cand.prev_close == 100.0


def test_prefilter_rejects_small_gap():
    closes = np.linspace(50, 100, 220)
    assert daily_prefilter("AAA", _daily(closes, 102.0), TODAY, 3.0, 3.0) is None


def test_prefilter_rejects_below_200_sma():
    closes = np.linspace(150, 100, 220)  # downtrend: close below the 200-SMA
    assert daily_prefilter("AAA", _daily(closes, 110.0), TODAY, 3.0, 3.0) is None


def test_prefilter_rejects_when_today_bar_missing():
    closes = np.linspace(50, 100, 220)
    stale = _daily(closes, 104.0, today=date(2026, 9, 24))
    assert daily_prefilter("AAA", stale, TODAY, 3.0, 3.0) is None


def test_rank_candidates_by_gap_desc_and_caps():
    cands = [GapCandidate(s, 1, 1, 1, g, 1) for s, g in [("A", 3.5), ("B", 9.0), ("C", 5.0)]]
    assert [c.symbol for c in rank_candidates(cands, 2)] == ["B", "C"]


# --- intraday helpers -------------------------------------------------------

def _intraday(rows):
    """rows: list of (HH:MM, open, high, low, close, volume) on TODAY."""
    idx = pd.DatetimeIndex([pd.Timestamp(f"{TODAY} {t}", tz=ET) for t, *_ in rows])
    data = [r[1:] for r in rows]
    return pd.DataFrame(data, columns=["open", "high", "low", "close", "volume"], index=idx)


def test_completed_bars_drops_forming_bar():
    bars = _intraday([("10:00", 1, 1, 1, 1, 1), ("10:05", 1, 1, 1, 1, 1)])
    now = pd.Timestamp(f"{TODAY} 10:07", tz=ET)
    assert len(completed_bars(bars, now, 5)) == 1


def test_split_session():
    bars = _intraday([("08:00", 1, 1, 1, 1, 1), ("09:30", 1, 1, 1, 1, 1), ("16:05", 1, 1, 1, 1, 1)])
    pre, rth = split_session(bars, TODAY)
    assert len(pre) == 1 and len(rth) == 1


def test_relative_volume_is_time_of_day_adjusted():
    # 14 prior days, each with 100 shares in the 09:30 bar and 900 later.
    hist_rows = []
    for d in pd.bdate_range(end=pd.Timestamp("2026-09-24"), periods=14):
        hist_rows.append((pd.Timestamp(f"{d.date()} 09:30", tz=ET), 100))
        hist_rows.append((pd.Timestamp(f"{d.date()} 15:00", tz=ET), 900))
    hist = pd.DataFrame({"volume": [v for _, v in hist_rows]}, index=pd.DatetimeIndex([t for t, _ in hist_rows]))
    today = _intraday([("09:30", 1, 1, 1, 1, 300)])
    # Same time of day: 300 today vs 100 average -> 3.0 (not 300/1000).
    assert relative_volume(today, hist, 14) == pytest.approx(3.0)


def test_relative_volume_none_without_enough_history():
    hist = _intraday([("09:30", 1, 1, 1, 1, 100)])
    today = _intraday([("09:35", 1, 1, 1, 1, 100)])
    assert relative_volume(today, hist, 14) is None


# --- entry (D1, I1, I2, I3) -------------------------------------------------

CAND = GapCandidate("AAA", prev_close=100, prev_high=101, today_open=104, gap_pct=4.0, sma200=90)
PRE = _intraday([("08:00", 103, 105, 103, 104, 1000)])


def test_entry_all_filters_pass():
    rth = _intraday([("09:30", 104, 105.5, 103, 105, 1), ("09:35", 105, 106.2, 104.5, 106, 1)])
    check = check_entry(CAND, PRE, rth, rvol=2.5, min_rvol=2.0)
    assert check.ok, check.reason
    assert check.price == 106 and check.low_of_day == 103


def test_entry_requires_new_high_of_day():
    rth = _intraday([("09:30", 104, 107, 103, 105, 1), ("09:35", 105, 106.2, 104.5, 106, 1)])
    assert "I2" in check_entry(CAND, PRE, rth, 2.5, 2.0).reason


def test_entry_requires_above_premarket_high():
    pre = _intraday([("08:00", 103, 110, 103, 104, 1000)])
    rth = _intraday([("09:30", 104, 105.5, 103, 105, 1), ("09:35", 105, 106.2, 104.5, 106, 1)])
    assert "I1" in check_entry(CAND, pre, rth, 2.5, 2.0).reason


def test_entry_requires_relative_volume():
    rth = _intraday([("09:30", 104, 105.5, 103, 105, 1), ("09:35", 105, 106.2, 104.5, 106, 1)])
    assert "I3" in check_entry(CAND, PRE, rth, 1.5, 2.0).reason


def test_entry_requires_above_prior_day_high():
    cand = GapCandidate("AAA", 100, prev_high=120, today_open=104, gap_pct=4.0, sma200=90)
    rth = _intraday([("09:30", 104, 105.5, 103, 105, 1), ("09:35", 105, 106.2, 104.5, 106, 1)])
    assert "D1" in check_entry(cand, PRE, rth, 2.5, 2.0).reason


# --- stop & sizing ----------------------------------------------------------

def test_initial_stop_is_lod_minus_1pct():
    assert initial_stop(entry=106, low_of_day=103, lod_buffer_pct=0.01, max_stop_pct=0.10) == 101.97


def test_initial_stop_capped_at_10pct():
    assert initial_stop(entry=100, low_of_day=80, lod_buffer_pct=0.01, max_stop_pct=0.10) == 90.0


def test_share_quantity_respects_risk_and_notional_cap():
    # risk: 1% of 1M = 10k / R=4 -> 2500 shares; cap: 2000/100 -> 20 shares.
    assert share_quantity(1_000_000, 0.01, entry=100, stop=96, max_position_value=2000) == 20
    # small account: 1% of 10k = 100 / R=4 -> 25 shares, under the 2000/50=40 cap
    assert share_quantity(10_000, 0.01, entry=50, stop=46, max_position_value=2000) == 25


# --- swing low & exits ------------------------------------------------------

def test_latest_swing_low_needs_two_bars_each_side():
    lows = [10, 9, 8, 9, 10, 7, 8]  # 8@idx2 confirmed; 7@idx5 has only 1 bar after
    bars = pd.DataFrame({"low": lows})
    assert latest_swing_low(bars) == 8


FLAT = time(15, 51)


def _plan(pos, price, swing=None, now=time(11, 0)):
    return plan_exit(pos, price, now, swing, FLAT, partial_r=0.75, partial_fraction=0.3333, breakeven_r=1.0)


def test_exit_eod_flatten():
    pos = PositionState(entry=100, initial_stop=96, stop=96, qty=9, remaining=9)
    assert _plan(pos, 101, now=time(15, 51)).flatten_reason == "eod_flatten"


def test_exit_stop_hit_safety_net():
    pos = PositionState(entry=100, initial_stop=96, stop=96, qty=9, remaining=9)
    assert _plan(pos, 95.9).flatten_reason == "stop_hit"


def test_partial_at_075R_sells_a_third():
    pos = PositionState(entry=100, initial_stop=96, stop=96, qty=9, remaining=9)
    plan = _plan(pos, 103)  # +0.75R
    assert plan.partial_triggered and plan.partial_qty == 2  # floor(9 * 0.3333)
    assert plan.new_stop is None and not plan.breakeven_reached


def test_partial_not_repeated():
    pos = PositionState(entry=100, initial_stop=96, stop=96, qty=9, remaining=7, partial_taken=True)
    assert not _plan(pos, 103).partial_triggered


def test_breakeven_at_1R():
    pos = PositionState(entry=100, initial_stop=96, stop=96, qty=9, remaining=7, partial_taken=True)
    plan = _plan(pos, 104)
    assert plan.breakeven_reached and plan.new_stop == 100


def test_trail_to_swing_low_after_breakeven_only_ratchets_up():
    pos = PositionState(100, 96, stop=100, qty=9, remaining=7, partial_taken=True, breakeven=True)
    assert _plan(pos, 108, swing=103).new_stop == 102.99
    assert _plan(pos, 108, swing=99).new_stop is None  # below current stop: no change


def test_no_trailing_before_breakeven():
    pos = PositionState(entry=100, initial_stop=96, stop=96, qty=9, remaining=9)
    assert _plan(pos, 101, swing=99).new_stop is None


def test_partial_leaves_at_least_one_share():
    pos = PositionState(entry=100, initial_stop=96, stop=96, qty=1, remaining=1)
    plan = _plan(pos, 103)
    assert plan.partial_triggered and plan.partial_qty == 0
