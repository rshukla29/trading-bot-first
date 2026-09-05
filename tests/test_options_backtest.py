from datetime import time

import pandas as pd

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
