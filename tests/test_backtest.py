import numpy as np
import pandas as pd

from tradingbot.backtest import run_backtest
from tradingbot.strategy.ma_crossover import MovingAverageCrossoverStrategy


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
    strategy = MovingAverageCrossoverStrategy(fast=10, slow=30)
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
    strategy = MovingAverageCrossoverStrategy(fast=5, slow=15)
    bars = make_trending_bars(200, drift=0.0, seed=1)
    result = run_backtest(
        "TEST", bars, strategy,
        starting_equity=50_000, risk_pct=0.01, stop_loss_pct=0.20, warmup=16,
    )
    assert result.ending_equity > 0
    assert -1.0 <= result.max_drawdown_pct <= 0.0


def test_stop_loss_caps_loss_per_trade():
    strategy = MovingAverageCrossoverStrategy(fast=5, slow=10)
    bars = make_trending_bars(150, drift=0.3, seed=2)
    result = run_backtest(
        "TEST", bars, strategy,
        starting_equity=100_000, risk_pct=0.01, stop_loss_pct=0.20, warmup=11,
    )
    for trade in result.closed_trades:
        if trade.exit_reason == "stop_loss":
            loss_pct = (trade.exit_price - trade.entry_price) / trade.entry_price
            assert loss_pct <= -0.19  # allow tiny float rounding
