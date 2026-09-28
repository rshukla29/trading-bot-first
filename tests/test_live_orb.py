from datetime import time

from tradingbot.options.live_orb import determine_exit_reason


def _base_kwargs(**overrides):
    kwargs = dict(
        now_time=time(10, 0),
        current_premium=1.00,
        entry_premium=1.00,
        stop_loss_pct_of_premium=0.30,
        profit_target_pct=0.50,
        spot=102.0,  # above range_high: the call breakout already happened, still holding
        right="C",
        range_low=99.0,
        range_high=101.0,
        exit_on_range_reentry=True,
    )
    kwargs.update(overrides)
    return kwargs


def test_holds_when_nothing_triggers():
    assert determine_exit_reason(**_base_kwargs()) is None


def test_eod_flatten_takes_priority_over_everything():
    # Stop-loss condition is also true, but eod_flatten must win (matches
    # options/backtest.py's exit priority order exactly).
    assert determine_exit_reason(**_base_kwargs(
        now_time=time(15, 55), current_premium=0.50,
    )) == "eod_flatten"


def test_stop_loss_triggers_at_30pct_down():
    assert determine_exit_reason(**_base_kwargs(current_premium=0.70)) == "stop_loss"
    assert determine_exit_reason(**_base_kwargs(current_premium=0.71)) is None


def test_profit_target_triggers_at_50pct_up():
    assert determine_exit_reason(**_base_kwargs(current_premium=1.50)) == "profit_target"
    assert determine_exit_reason(**_base_kwargs(current_premium=1.49)) is None


def test_profit_target_disabled_when_none():
    assert determine_exit_reason(**_base_kwargs(current_premium=5.00, profit_target_pct=None)) is None


def test_range_reentry_call_side():
    # A call breakout fails if the underlying falls back into (or below) the range.
    assert determine_exit_reason(**_base_kwargs(right="C", spot=101.0)) == "range_reentry"
    assert determine_exit_reason(**_base_kwargs(right="C", spot=101.5)) is None


def test_range_reentry_put_side():
    assert determine_exit_reason(**_base_kwargs(right="P", spot=99.0)) == "range_reentry"
    assert determine_exit_reason(**_base_kwargs(right="P", spot=98.5)) is None


def test_range_reentry_disabled():
    assert determine_exit_reason(**_base_kwargs(right="C", spot=101.0, exit_on_range_reentry=False)) is None


def test_step_stop_exits_when_premium_falls_to_locked_step():
    # entry 1.00, peak reached +21% -> step stop 1.20
    assert determine_exit_reason(**_base_kwargs(current_premium=1.20, step_stop_premium=1.20)) == "step_stop"
    assert determine_exit_reason(**_base_kwargs(current_premium=1.21, step_stop_premium=1.20)) is None


def test_step_stop_none_means_disabled():
    assert determine_exit_reason(**_base_kwargs(current_premium=1.05, step_stop_premium=None)) is None


def test_stop_loss_still_outranks_step_stop():
    assert determine_exit_reason(**_base_kwargs(current_premium=0.70, step_stop_premium=1.10)) == "stop_loss"


def test_vwap_cross_exits_call_on_close_below_vwap():
    assert determine_exit_reason(**_base_kwargs(right="C", bar_close=99.9, vwap=100.0)) == "vwap_cross"
    assert determine_exit_reason(**_base_kwargs(right="C", bar_close=100.1, vwap=100.0)) is None


def test_vwap_cross_exits_put_on_close_above_vwap():
    assert determine_exit_reason(**_base_kwargs(right="P", spot=98.0, bar_close=100.1, vwap=100.0)) == "vwap_cross"
    assert determine_exit_reason(**_base_kwargs(right="P", spot=98.0, bar_close=99.9, vwap=100.0)) is None


def test_stop_loss_can_be_disabled():
    assert determine_exit_reason(**_base_kwargs(current_premium=0.10, use_stop_loss=False)) is None


def test_eod_flatten_still_applies_with_vwap_only():
    assert determine_exit_reason(**_base_kwargs(
        now_time=time(15, 55), use_stop_loss=False, bar_close=101.0, vwap=100.0,
    )) == "eod_flatten"


# --- profit lock (keep half the peak gain once up 100%) -----------------------

from tradingbot.risk import profit_lock_floor  # noqa: E402


def test_profit_lock_floor_only_after_doubling():
    assert profit_lock_floor(1.00, 1.99, 1.0, 0.5) is None
    assert profit_lock_floor(1.00, 2.00, 1.0, 0.5) == 1.50
    assert profit_lock_floor(1.00, 2.40, 1.0, 0.5) == 1.70  # rises with the peak
    assert profit_lock_floor(1.00, 5.00, 0.0, 0.5) is None  # 0 disables


def test_profit_lock_exits_when_premium_gives_back_half_the_peak_gain():
    assert determine_exit_reason(**_base_kwargs(current_premium=1.70, lock_floor_premium=1.70,
                                                use_stop_loss=False, profit_target_pct=None)) == "profit_lock"
    assert determine_exit_reason(**_base_kwargs(current_premium=1.71, lock_floor_premium=1.70,
                                                use_stop_loss=False, profit_target_pct=None)) is None
