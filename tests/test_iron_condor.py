import pandas as pd

from tradingbot.options.iron_condor import IronCondorConfig, run_iron_condor_backtest


def make_bars(closes: list[float], start: str = "2024-01-02") -> pd.DataFrame:
    dates = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.003 for c in closes],
            "low": [c * 0.997 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * len(closes),
        },
        index=dates,
    )


# Small, realistic noise to warm up realized vol without a single move
# dominating the estimate (same reasoning as the ORB backtest tests).
PATTERN = [100.0, 100.3, 99.8, 100.2, 99.9]
PADDING_10 = PATTERN * 2
PADDING_20 = PATTERN * 4


def run(bars: pd.DataFrame, config: IronCondorConfig, **overrides):
    kwargs = dict(starting_equity=100_000.0, risk_pct_per_trade=0.02, warmup=config.vol_window + 2)
    kwargs.update(overrides)
    return run_iron_condor_backtest("TEST", bars, config, **kwargs)


def test_entry_opens_a_correctly_structured_condor():
    bars = make_bars(PADDING_20 + [100.0] * 30)
    config = IronCondorConfig(dte_days=20, dte_forced_close=5, entry_interval_days=100, max_concurrent_positions=1, vol_window=5)
    result = run(bars, config)
    assert len(result.trades) >= 1
    trade = result.trades[0]
    # Standard iron condor ordering: both long wings outside their short strikes.
    assert trade.call_long_strike > trade.call_short_strike > trade.put_short_strike > trade.put_long_strike
    assert trade.credit_received > 0
    assert trade.max_loss_per_contract > 0
    assert trade.contracts > 0


def test_profit_target_exit_on_theta_decay():
    # Underlying stays flat after entry: both spreads lose extrinsic value
    # over time, and cost-to-close should fall to 50% of the credit
    # received well before the (generous, 15-day-out) forced-close point.
    bars = make_bars(PADDING_20 + [100.0] * 30)
    config = IronCondorConfig(dte_days=20, dte_forced_close=5, entry_interval_days=100, max_concurrent_positions=1, vol_window=5)
    result = run(bars, config)
    closed = result.closed_trades
    assert len(closed) == 1
    assert closed[0].exit_reason == "profit_target"
    assert closed[0].pnl > 0


def test_stop_loss_exit_on_directional_shock():
    # A sharp, sustained move immediately after entry pushes the call side
    # deep in-the-money, spiking the cost to close well past 2x credit.
    bars = make_bars(PADDING_10 + [130.0] * 10)
    config = IronCondorConfig(dte_days=20, dte_forced_close=5, entry_interval_days=100, max_concurrent_positions=1, vol_window=5)
    result = run(bars, config)
    closed = result.closed_trades
    assert len(closed) == 1
    assert closed[0].exit_reason == "stop_loss"
    assert closed[0].pnl < 0


def test_dte_forced_close_fires_before_expiry():
    # dte_forced_close=9 on a 10-DTE condor leaves only ~1 day before the
    # forced exit fires — not enough time for meaningful decay, so this
    # isolates the time-based exit from the profit-target path.
    bars = make_bars(PADDING_10 + [100.0] * 6)
    config = IronCondorConfig(dte_days=10, dte_forced_close=9, entry_interval_days=100, max_concurrent_positions=1, vol_window=5)
    result = run(bars, config)
    closed = result.closed_trades
    assert len(closed) == 1
    assert closed[0].exit_reason == "dte_forced_close"
    days_held = (closed[0].exit_date - closed[0].entry_date).days
    assert days_held <= 2


def test_entry_cadence_is_respected():
    # entry_interval_days=5 (business days): a long, flat run should
    # produce entries spaced at least 5 trading bars apart, never closer.
    bars = make_bars(PADDING_10 + [100.0] * 40)
    config = IronCondorConfig(
        dte_days=60, dte_forced_close=5, entry_interval_days=5, max_concurrent_positions=10, vol_window=5,
    )
    result = run(bars, config)
    entry_dates = sorted(t.entry_date for t in result.trades)
    assert len(entry_dates) >= 2
    gaps_in_bdays = [len(pd.bdate_range(a, b)) - 1 for a, b in zip(entry_dates, entry_dates[1:])]
    assert all(gap >= 5 for gap in gaps_in_bdays)


def test_max_concurrent_positions_caps_open_trades():
    # With a long-lived condor (60 DTE) and a fast cadence, concurrency
    # should never exceed the configured cap at any point in time.
    bars = make_bars(PADDING_10 + [100.0 + 0.05 * i for i in range(40)])
    config = IronCondorConfig(
        dte_days=60, dte_forced_close=5, entry_interval_days=5, max_concurrent_positions=2, vol_window=5,
    )
    result = run(bars, config)

    events = []
    for t in result.trades:
        events.append((t.entry_date, 1))
        if t.exit_date is not None:
            events.append((t.exit_date, -1))
    events.sort()

    concurrent = 0
    for _, delta in events:
        concurrent += delta
        assert concurrent <= config.max_concurrent_positions


def test_min_vol_percentile_blocks_entries_when_vol_is_low():
    # A high min_vol_percentile requirement on a calm, low-noise series
    # should block essentially all entries.
    bars = make_bars(PADDING_10 * 20)  # long, uniformly calm history
    config = IronCondorConfig(
        dte_days=20, dte_forced_close=5, entry_interval_days=3, max_concurrent_positions=5,
        vol_window=5, min_vol_percentile=0.99, vol_percentile_lookback=50,
    )
    result = run(bars, config, warmup=config.vol_percentile_lookback + config.vol_window + 2)
    assert len(result.trades) == 0
