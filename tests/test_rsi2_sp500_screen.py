from types import SimpleNamespace

import numpy as np
import pandas as pd

from tradingbot.run_rsi2_bot import order_for_action
from tradingbot.sp500_scanner import screen_rsi2_buys, to_ibkr_symbol
from tradingbot.strategy.rsi2_mean_reversion import RSI2MeanReversionStrategy


def _bars(closes):
    idx = pd.bdate_range("2025-01-01", periods=len(closes))
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1_000_000}, index=idx)


def _uptrend_then_drop(drop_pcts):
    """Long uptrend (well above the 200-SMA), then consecutive % drops — a
    textbook RSI(2) oversold dip."""
    closes = list(np.linspace(50, 100, 260))
    for pct in drop_pcts:
        closes.append(closes[-1] * (1 - pct))
    return _bars(closes)


STRATEGY = RSI2MeanReversionStrategy(trend_ma=200, rsi_period=2, buy_threshold=5.0, sell_threshold=65.0)


def test_to_ibkr_symbol():
    assert to_ibkr_symbol("BRK-B") == "BRK B"
    assert to_ibkr_symbol("AAPL") == "AAPL"


def test_screen_returns_only_buys_most_oversold_first():
    bars = {
        "MILD": _uptrend_then_drop([0.02, 0.02]),      # oversold (RSI(2) < 5)
        "DEEP": _uptrend_then_drop([0.03, 0.04]),      # more oversold -> first
        "FLAT": _bars(list(np.linspace(50, 100, 262))),  # still rising: no BUY
    }
    hits = screen_rsi2_buys(STRATEGY, 2, exclude=set(), max_candidates=10, bars_by_symbol=bars)
    assert hits == ["DEEP", "MILD"]


def test_screen_respects_exclude_and_cap():
    bars = {s: _uptrend_then_drop([0.02, 0.02]) for s in ("AAA", "BBB", "CCC")}
    hits = screen_rsi2_buys(STRATEGY, 2, exclude={"BBB"}, max_candidates=1, bars_by_symbol=bars)
    assert len(hits) == 1 and hits[0] != "BBB"


def test_screen_maps_yahoo_class_shares_to_ibkr():
    bars = {"BRK-B": _uptrend_then_drop([0.02, 0.02])}
    assert screen_rsi2_buys(STRATEGY, 2, exclude=set(), max_candidates=5, bars_by_symbol=bars) == ["BRK B"]
    # Excluded by its IBKR name (how the watchlists/positions spell it).
    assert screen_rsi2_buys(STRATEGY, 2, exclude={"BRK B"}, max_candidates=5, bars_by_symbol=bars) == []


def test_order_for_action_exits_first_then_lowest_rsi_buys():
    buy = SimpleNamespace(action="BUY")
    hold = SimpleNamespace(action="HOLD")
    evaluated = [
        ("MILD", _uptrend_then_drop([0.02, 0.02]), buy),
        ("OTHER", _bars(list(np.linspace(50, 100, 262))), hold),
        ("DEEP", _uptrend_then_drop([0.03, 0.04]), buy),
        ("HELD", _bars(list(np.linspace(50, 100, 262))), hold),
    ]
    order = [s for s, _, _ in order_for_action(evaluated, held={"HELD"}, rsi_period=2)]
    assert order == ["HELD", "DEEP", "MILD", "OTHER"]
