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
