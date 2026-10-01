from datetime import time

import pandas as pd
import pytest

from tradingbot.options.backtest import run_options_backtest
from tradingbot.options.orb_strategy import OpeningRangeBreakoutStrategy


def make_bars(closes: list[float], start: str) -> pd.DataFrame:
    dates = pd.date_range(start, periods=len(closes), freq="5min")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * len(closes),
        },
        index=dates,
    )


# 25 bars of small, realistic noise (no single large move) to warm up a
# 20-bar realized-vol window before the test day starts. A too-small vol
# window lets a single extreme bar (the breakout itself) dominate the
# estimate and produce wildly overpriced options right at entry — this
# padding exists specifically to avoid that failure mode.
_PATTERN = [100.0, 100.2, 99.9, 100.1, 99.95]
PADDING = make_bars(_PATTERN * 5, start="2024-01-02 09:30")

# Opening range for the test day: closes [99.8, 100.3, 100.0] ->
# highs [100.3, 100.8, 100.5], lows [99.3, 99.8, 99.5]
# -> range_low=99.3, range_high=100.8
RANGE = [99.8, 100.3, 100.0]


def strategy() -> OpeningRangeBreakoutStrategy:
    return OpeningRangeBreakoutStrategy(opening_range_minutes=15, bar_minutes=5)


def run(bars: pd.DataFrame, **overrides):
    kwargs = dict(
        symbol="SPY",
        bars=bars,
        strategy=strategy(),
        starting_equity=100_000.0,
        risk_pct_per_trade=0.01,
        stop_loss_pct_of_premium=0.9,
        profit_target_pct=None,
        dte_days=0,
        otm_pct=0.0,
        strike_increment=1.0,
        vol_window=20,
        iv_premium_multiplier=1.2,
        exit_on_range_reentry=False,
    )
    kwargs.update(overrides)
    return run_options_backtest(**kwargs)


def test_breakout_opens_a_call_position():
    day = make_bars(RANGE + [105.0], start="2024-01-03 09:30")
    bars = pd.concat([PADDING, day])
    result = run(bars)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.right == "C"
    assert trade.strike == 105.0
    assert trade.entry_premium > 0.01


def test_eod_flatten_closes_position_at_session_close():
    # otm_pct=-0.05 buys an in-the-money call (strike below spot) so
    # intrinsic value gives the premium a floor well above any reasonable
    # stop threshold — isolates the time-based EOD flatten from theta
    # decay, which on a flat underlying otherwise erodes an ATM option
    # toward zero and would trip the stop-loss on its own well before close.
    tail = [105.0] * 74  # carries the day from 09:45 out to 15:55
    day = make_bars(RANGE + [105.0] + tail, start="2024-01-03 09:30")
    bars = pd.concat([PADDING, day])
    result = run(bars, otm_pct=-0.05)
    closed = result.closed_trades
    assert len(closed) == 1
    assert closed[0].exit_reason == "eod_flatten"
    assert closed[0].exit_time.time() == time(15, 55)


def test_stop_loss_exit_on_sharp_reversal():
    # Breakout to 105, then the underlying craters to 85 — the 105-strike
    # call should lose most of its value.
    day = make_bars(RANGE + [105.0, 85.0], start="2024-01-03 09:30")
    bars = pd.concat([PADDING, day])
    result = run(bars, stop_loss_pct_of_premium=0.5)
    closed = result.closed_trades
    assert len(closed) == 1
    assert closed[0].exit_reason == "stop_loss"
    assert closed[0].pnl < 0


def test_profit_target_exit_on_strong_follow_through():
    day = make_bars(RANGE + [105.0, 140.0], start="2024-01-03 09:30")
    bars = pd.concat([PADDING, day])
    result = run(bars, profit_target_pct=0.3, stop_loss_pct_of_premium=0.9)
    closed = result.closed_trades
    assert len(closed) == 1
    assert closed[0].exit_reason == "profit_target"
    assert closed[0].pnl > 0


def test_range_reentry_exit_when_breakout_fails():
    # Breakout to 105, then a pullback to 100.2 — back below range_high
    # (100.8), invalidating the breakout, well short of a 90% stop.
    day = make_bars(RANGE + [105.0, 100.2], start="2024-01-03 09:30")
    bars = pd.concat([PADDING, day])
    result = run(bars, exit_on_range_reentry=True, stop_loss_pct_of_premium=0.9)
    closed = result.closed_trades
    assert len(closed) == 1
    assert closed[0].exit_reason == "range_reentry"


def test_breakdown_opens_a_put_position():
    day = make_bars(RANGE + [95.0], start="2024-01-03 09:30")
    bars = pd.concat([PADDING, day])
    result = run(bars)
    assert len(result.trades) == 1
    assert result.trades[0].right == "P"


def test_equity_curve_length_matches_evaluated_bars():
    day = make_bars(RANGE + [105.0, 105.0, 105.0], start="2024-01-03 09:30")
    bars = pd.concat([PADDING, day])
    warmup = max(strategy().min_history, 20 + 1)
    result = run(bars, warmup=warmup)
    assert len(result.equity_curve) == len(bars) - warmup


# --- VWAP exit, step stop, costs (ORB exit comparison) ------------------------

def test_vwap_exit_when_call_closes_below_session_vwap():
    # Session VWAP after RANGE + 105 is ~101.3 (equal volume); 101.0 closes
    # below it while still above range_high (100.8), so range re-entry
    # wouldn't have fired — only the VWAP exit does.
    day = make_bars(RANGE + [105.0, 101.0], start="2024-01-03 09:30")
    result = run(pd.concat([PADDING, day]), vwap_exit=True, stop_loss_pct_of_premium=0.99)
    closed = result.closed_trades
    assert len(closed) == 1 and closed[0].exit_reason == "vwap_cross"


def test_vwap_exit_holds_while_price_stays_above_vwap():
    day = make_bars(RANGE + [105.0, 104.0], start="2024-01-03 09:30")
    result = run(pd.concat([PADDING, day]), vwap_exit=True, stop_loss_pct_of_premium=0.99)
    assert result.closed_trades == []


def test_step_stop_locks_profit_after_spike_and_fade():
    day = make_bars(RANGE + [105.0, 106.5, 105.4], start="2024-01-03 09:30")
    result = run(pd.concat([PADDING, day]), step_stop_pct=0.10, stop_loss_pct_of_premium=0.99)
    closed = result.closed_trades
    assert len(closed) == 1 and closed[0].exit_reason == "step_stop"
    assert closed[0].pnl > 0


def test_costs_reduce_pnl_by_slippage_and_commission():
    day = make_bars(RANGE + [105.0, 101.0], start="2024-01-03 09:30")
    bars = pd.concat([PADDING, day])
    free = run(bars, vwap_exit=True, stop_loss_pct_of_premium=0.99).closed_trades[0]
    costly = run(bars, vwap_exit=True, stop_loss_pct_of_premium=0.99,
                 slippage_per_share=0.02, commission_per_contract=0.65).closed_trades[0]
    assert costly.entry_premium == pytest.approx(free.entry_premium + 0.02)
    assert costly.fees == pytest.approx(2 * 0.65 * costly.contracts)
    assert costly.pnl < free.pnl


def test_max_contracts_caps_position_size():
    day = make_bars(RANGE + [105.0], start="2024-01-03 09:30")
    result = run(pd.concat([PADDING, day]), max_contracts=2)
    assert result.trades[0].contracts == 2


# --- re-entry, daily loss limit, entry cutoff --------------------------------

def test_reentry_after_exit_when_trend_makes_new_high():
    # Breakout (105) -> VWAP exit (101) -> new high of day (107) = re-entry.
    day = make_bars(RANGE + [105.0, 101.0, 107.0], start="2024-01-03 09:30")
    bars = pd.concat([PADDING, day])
    no_re = run(bars, vwap_exit=True, stop_loss_pct_of_premium=0.99)
    with_re = run(bars, vwap_exit=True, stop_loss_pct_of_premium=0.99, max_reentries=1)
    assert len(no_re.trades) == 1
    assert len(with_re.trades) == 2 and with_re.trades[1].right == "C"


def test_no_reentry_without_a_new_session_extreme():
    day = make_bars(RANGE + [105.0, 101.0, 104.0], start="2024-01-03 09:30")  # 104 < prior high 105.5
    result = run(pd.concat([PADDING, day]), vwap_exit=True, stop_loss_pct_of_premium=0.99, max_reentries=1)
    assert len(result.trades) == 1


def test_daily_loss_limit_blocks_further_entries():
    # Weak breakout (101.2) fails back below VWAP (100.2) at a loss, then a
    # new high (107) would normally be a re-entry.
    day = make_bars(RANGE + [101.2, 100.2, 107.0], start="2024-01-03 09:30")
    bars = pd.concat([PADDING, day])
    kwargs = dict(vwap_exit=True, stop_loss_pct_of_premium=0.99, max_reentries=1)
    assert run(bars, **kwargs).closed_trades[0].pnl < 0
    assert len(run(bars, **kwargs).trades) == 2
    assert len(run(bars, **kwargs, daily_loss_limit=1.0).trades) == 1


def test_last_entry_time_blocks_late_entries():
    day = make_bars(RANGE + [105.0], start="2024-01-03 09:30")  # breakout bar at 09:45
    result = run(pd.concat([PADDING, day]), last_entry_time=time(9, 45))
    assert result.trades == []


def test_max_trades_per_day_and_weekday_filter():
    day = make_bars(RANGE + [105.0, 101.0, 95.0], start="2024-01-03 09:30")  # Wednesday: call then put
    bars = pd.concat([PADDING, day])
    assert len(run(bars, vwap_exit=True, stop_loss_pct_of_premium=0.99).trades) == 2
    assert len(run(bars, vwap_exit=True, stop_loss_pct_of_premium=0.99, max_trades_per_day=1).trades) == 1
    assert run(bars, trade_weekdays=(0, 4)).trades == []  # Mon/Fri only


def test_two_bar_confirmation_enters_on_second_close():
    day = make_bars(RANGE + [105.0, 105.5], start="2024-01-03 09:30")
    result = run(pd.concat([PADDING, day]), confirm_bars=2)
    assert len(result.trades) == 1 and result.trades[0].entry_time.strftime("%H:%M") == "09:50"


def test_min_range_filter():
    day = make_bars(RANGE + [105.0], start="2024-01-03 09:30")  # range ~1.5 on ~100 = 1.5%
    assert run(pd.concat([PADDING, day]), min_range_pct=0.02).trades == []
    assert len(run(pd.concat([PADDING, day]), min_range_pct=0.01).trades) == 1
