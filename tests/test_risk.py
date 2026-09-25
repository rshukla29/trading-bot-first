from tradingbot.risk import DailyLossKillSwitch, can_open_new_position, position_size


def test_position_size_basic():
    # 100k equity, 1% risk = $1000 risk budget. Price $100, 20% stop = $20/share risk.
    # 1000 / 20 = 50 shares.
    assert position_size(equity=100_000, price=100, risk_pct=0.01, stop_loss_pct=0.20) == 50


def test_position_size_zero_on_bad_inputs():
    assert position_size(equity=100_000, price=0, risk_pct=0.01, stop_loss_pct=0.20) == 0
    assert position_size(equity=100_000, price=100, risk_pct=0.01, stop_loss_pct=0) == 0


def test_position_size_capped_by_max_position_value():
    # Risk formula alone would give 1,000,000 * 0.01 / (24.86 * 0.10) ~= 4023 shares
    # (the real BRZE case) — a $2,000 notional cap should bring that down to 80 shares.
    qty = position_size(
        equity=1_000_000, price=24.86, risk_pct=0.01, stop_loss_pct=0.10, max_position_value=2000,
    )
    assert qty == 80
    assert qty * 24.86 <= 2000


def test_position_size_uncapped_when_max_position_value_none():
    assert position_size(equity=100_000, price=100, risk_pct=0.01, stop_loss_pct=0.20) == 50


def test_kill_switch_trips_on_threshold():
    ks = DailyLossKillSwitch(max_daily_loss_pct=0.03, starting_equity=100_000)
    assert not ks.is_tripped()
    ks.record_pnl(-2_500)
    assert not ks.is_tripped()
    ks.record_pnl(-600)
    assert ks.is_tripped()


def test_kill_switch_reset():
    ks = DailyLossKillSwitch(max_daily_loss_pct=0.03, starting_equity=100_000)
    ks.record_pnl(-5_000)
    assert ks.is_tripped()
    ks.reset(starting_equity=95_000)
    assert not ks.is_tripped()
    assert ks.realized_pnl_today == 0.0


def test_can_open_new_position():
    assert can_open_new_position(current_open_positions=3, max_positions=5)
    assert not can_open_new_position(current_open_positions=5, max_positions=5)


# --- stepped profit-locking stop ---------------------------------------------

from tradingbot.risk import step_trailing_stop  # noqa: E402


def test_step_stop_user_example():
    # 100 shares @ $10 = $1,000: +11% -> stop $1,100 total; +21% -> $1,200.
    assert step_trailing_stop(10.0, 11.10) == 11.00
    assert step_trailing_stop(10.0, 12.10) == 12.00
    assert step_trailing_stop(10.0, 13.10) == 13.00


def test_step_stop_none_below_first_step():
    assert step_trailing_stop(10.0, 10.00) is None
    assert step_trailing_stop(10.0, 10.50) is None
    assert step_trailing_stop(10.0, 11.09) is None  # +10.9%: buffer not yet cleared


def test_step_stop_between_steps_stays_on_lower_step():
    assert step_trailing_stop(10.0, 11.90) == 11.00  # +19%
    assert step_trailing_stop(10.0, 12.09) == 11.00  # +20.9%


def test_step_stop_on_a_loss_is_none():
    assert step_trailing_stop(10.0, 8.00) is None
