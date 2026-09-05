import pandas as pd

from tradingbot.strategy.rsi2_mean_reversion import RSI2MeanReversionStrategy


def make_bars(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2023-01-02", periods=len(closes), freq="B")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.005 for c in closes],
            "low": [c * 0.995 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * len(closes),
        },
        index=dates,
    )


def strategy(**overrides) -> RSI2MeanReversionStrategy:
    kwargs = dict(trend_ma=50, rsi_period=2, buy_threshold=5.0, sell_threshold=65.0)
    kwargs.update(overrides)
    return RSI2MeanReversionStrategy(**kwargs)


def test_insufficient_history_holds():
    bars = make_bars([100.0] * 5)
    assert strategy(trend_ma=10).generate_signal("TEST", bars).action == "HOLD"


def test_min_history():
    assert strategy(trend_ma=200, rsi_period=2).min_history == 201


def test_buy_on_oversold_pullback_within_uptrend():
    # A long, gentle uptrend (58 bars) means the 50-SMA trend filter is
    # dominated by the sustained rally, not by a short pullback — matching
    # why real Connors RSI(2) uses a 200-day filter: it should reflect the
    # long-term trend, not react to the 2-day dip that triggers the entry.
    # A sharp final 2-day drop pushes RSI(2) to an extreme low while price
    # is still well above that 50-SMA.
    uptrend = [100.0 + 0.5 * i for i in range(58)]
    pullback = [uptrend[-1] - 4.0, uptrend[-1] - 8.0]
    bars = make_bars(uptrend + pullback)
    signal = strategy().generate_signal("TEST", bars)
    assert signal.action == "BUY"
    assert "RSI(2)" in signal.reason


def test_no_buy_when_oversold_but_below_trend():
    # A genuine downtrend: price craters and ends up BELOW its own 50-SMA
    # even though RSI(2) is deeply oversold — the "falling knife" case the
    # trend filter exists to block.
    downtrend = [100.0 - 0.5 * i for i in range(58)]
    crash = [downtrend[-1] - 15.0, downtrend[-1] - 30.0]
    bars = make_bars(downtrend + crash)
    signal = strategy().generate_signal("TEST", bars)
    assert signal.action == "HOLD"
    assert "trend filter" in signal.reason


def test_exit_on_overbought_rsi_regardless_of_trend():
    # A long, mild base, then a sharp 2-day rally that pushes RSI(2) to 100
    # — exit, computed independently of the trend filter.
    base = [100.0 + 0.1 * i for i in range(58)]
    rally = [base[-1] + 1.0, base[-1] + 6.0]
    bars = make_bars(base + rally)
    signal = strategy().generate_signal("TEST", bars)
    assert signal.action == "EXIT"
    assert "above 65" in signal.reason


def test_fib_zone_allows_pullback_in_golden_zone():
    # Same RSI(2)-oversold-in-uptrend setup as the base BUY test (RSI2=4.0,
    # above the 50-SMA trend). With fib_lookback=30 the retracement lands
    # at 58.7% — inside the classic 38.2%-61.8% golden zone — so the fib
    # confirmation should allow it.
    uptrend = [100.0 + 0.5 * i for i in range(58)]
    pullback = [uptrend[-1] - 4.0, uptrend[-1] - 8.0]
    bars = make_bars(uptrend + pullback)
    signal = strategy(fib_lookback=30).generate_signal("TEST", bars)
    assert signal.action == "BUY"
    assert "fib retracement" in signal.reason


def test_fib_zone_blocks_pullback_too_deep():
    # Identical price action, but a shorter fib_lookback (10) measures the
    # swing over a narrower recent window, putting the retracement at 93.5%
    # — a near-full round-trip back to the low, well outside the golden
    # zone — so the same RSI(2) signal should now be blocked.
    uptrend = [100.0 + 0.5 * i for i in range(58)]
    pullback = [uptrend[-1] - 4.0, uptrend[-1] - 8.0]
    bars = make_bars(uptrend + pullback)
    signal = strategy(fib_lookback=10).generate_signal("TEST", bars)
    assert signal.action == "HOLD"
    assert "golden zone" in signal.reason


def test_holds_with_no_setup():
    # A small up-down-up wiggle at the tail keeps RSI(2) in the mid-range —
    # neither oversold nor overbought.
    base = [100.0 + 0.1 * i for i in range(57)]
    wiggle = [base[-1] + 0.2, base[-1], base[-1] + 0.1]
    bars = make_bars(base + wiggle)
    signal = strategy().generate_signal("TEST", bars)
    assert signal.action == "HOLD"
    assert "no setup" in signal.reason
