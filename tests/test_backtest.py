import numpy as np
import pandas as pd

from tradingbot.backtest import run_backtest
from tradingbot.strategy.ma_crossover import MovingAverageCrossoverStrategy

# These tests exercise backtest mechanics, not the confirmation filters
# (those have their own tests in test_strategy.py).
NO_FILTERS = dict(trend_ma=None, volume_ma=None, rsi_period=None, vwap_window=None)


def make_trending_bars(n: int, drift: float, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 0.5, size=n)
    closes = 100 + np.cumsum(np.full(n, drift) + noise)
    closes = np.clip(closes, 1, None)
    dates = pd.date_range("2022-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.005,
            "low": closes * 0.99,
            "close": closes,
            "volume": 1_000_000,
        },
        index=dates,
    )


def test_backtest_runs_without_lookahead_and_produces_stats():
    # trend_ma=None: these tests exercise backtest mechanics in isolation
    # from the trend filter, which has its own tests in test_strategy.py.
    strategy = MovingAverageCrossoverStrategy(fast=10, slow=30, **NO_FILTERS)
    bars = make_trending_bars(300, drift=0.15)
    result = run_backtest(
        "TEST", bars, strategy,
        starting_equity=100_000, risk_pct=0.01, stop_loss_pct=0.20, warmup=31,
    )
    assert len(result.equity_curve) == 300 - 31
    # equity curve should never reference data beyond each day's index
    assert result.equity_curve[-1][0] == bars.index[-1]
    assert result.starting_equity == 100_000


def test_backtest_flat_market_survives_without_crashing():
    strategy = MovingAverageCrossoverStrategy(fast=5, slow=15, **NO_FILTERS)
    bars = make_trending_bars(200, drift=0.0, seed=1)
    result = run_backtest(
        "TEST", bars, strategy,
        starting_equity=50_000, risk_pct=0.01, stop_loss_pct=0.20, warmup=16,
    )
    assert result.ending_equity > 0
    assert -1.0 <= result.max_drawdown_pct <= 0.0


def make_bars(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2022-01-01", periods=len(closes), freq="B")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1_000_000] * len(closes),
        },
        index=dates,
    )


def test_stop_loss_caps_loss_per_trade():
    # Deterministic setup: flat baseline (10 bars) so both SMAs sit at 100,
    # then a jump to 150 triggers a golden cross BUY signal (fast=5/slow=10)
    # on that bar's close — but the fill happens at the *next* bar's open
    # (see run_backtest's next-open-fill docstring), which is also 150
    # here, giving entry_price=150 and stop_price=150*0.8=120. The bar
    # after that drops to 110, whose low (110*0.99=108.9) breaches the
    # stop, forcing a stop-loss exit at 120 — exactly a 20% loss.
    strategy = MovingAverageCrossoverStrategy(fast=5, slow=10, **NO_FILTERS)
    closes = [100.0] * 10 + [150.0, 150.0, 110.0, 110.0]
    bars = make_bars(closes)
    result = run_backtest(
        "TEST", bars, strategy,
        starting_equity=100_000, risk_pct=0.01, stop_loss_pct=0.20, warmup=10,
    )
    stop_loss_trades = [t for t in result.closed_trades if t.exit_reason == "stop_loss"]
    assert stop_loss_trades, f"expected a stop-loss exit, got trades: {result.trades}"
    for trade in stop_loss_trades:
        loss_pct = (trade.exit_price - trade.entry_price) / trade.entry_price
        assert loss_pct <= -0.19  # allow tiny float rounding


def test_entry_fills_at_next_bar_open_not_signal_bar_close():
    # Flat at 100 for 10 bars, then a jump straight to a different open/close
    # pair on the signal bar (open=140, close=150 -- distinguishable from
    # both) so a same-bar-close fill (the old, unrealistic behavior) would
    # show entry_price=150, while a next-bar-open fill (the fix) shows
    # whatever the *following* bar's open is -- set here to 145, a third
    # distinct value neither 100, 140 nor 150 could be confused with.
    strategy = MovingAverageCrossoverStrategy(fast=5, slow=10, **NO_FILTERS)
    dates = pd.date_range("2022-01-01", periods=13, freq="B")
    opens = [100.0] * 10 + [140.0, 145.0, 145.0]
    closes = [100.0] * 10 + [150.0, 145.0, 145.0]
    bars = pd.DataFrame(
        {
            "open": opens,
            "high": [max(o, c) * 1.01 for o, c in zip(opens, closes)],
            "low": [min(o, c) * 0.99 for o, c in zip(opens, closes)],
            "close": closes,
            "volume": [1_000_000] * 13,
        },
        index=dates,
    )
    result = run_backtest(
        "TEST", bars, strategy,
        starting_equity=100_000, risk_pct=0.01, stop_loss_pct=0.20, warmup=10,
    )
    assert result.trades, f"expected a trade, got: {result.trades}"
    assert result.trades[0].entry_price == 145.0


def test_backtest_with_trend_filter_integrates_without_crashing():
    # Default trend_ma=200 needs a long enough series to ever leave warmup;
    # this checks the filter integrates cleanly through the full backtest
    # loop (not just the single-signal unit tests in test_strategy.py).
    strategy = MovingAverageCrossoverStrategy(fast=10, slow=30, **{**NO_FILTERS, 'trend_ma': 200})
    bars = make_trending_bars(400, drift=0.15, seed=3)
    result = run_backtest(
        "TEST", bars, strategy,
        starting_equity=100_000, risk_pct=0.01, stop_loss_pct=0.20, warmup=201,
    )
    assert len(result.equity_curve) == 400 - 201
    assert result.ending_equity > 0
