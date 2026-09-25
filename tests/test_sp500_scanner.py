import numpy as np
import pandas as pd

from tradingbot.sp500_scanner import parse_constituents_html, scan, to_yahoo_symbol
from tradingbot.strategy.base import Signal, Strategy


def test_to_yahoo_symbol_converts_class_shares():
    assert to_yahoo_symbol("BRK.B") == "BRK-B"
    assert to_yahoo_symbol(" AAPL ") == "AAPL"


def test_parse_constituents_html():
    html = """
    <table id="constituents">
      <tr><th>Symbol</th><th>Security</th><th>GICS Sector</th></tr>
      <tr><td>AAPL</td><td>Apple Inc.</td><td>Information Technology</td></tr>
      <tr><td>BRK.B</td><td>Berkshire Hathaway</td><td>Financials</td></tr>
    </table>
    """
    df = parse_constituents_html(html)
    assert df["symbol"].tolist() == ["AAPL", "BRK-B"]
    assert df.loc[1, "sector"] == "Financials"


class _AlwaysBuy(Strategy):
    min_history = 5

    def generate_signal(self, symbol, bars):
        return Signal(symbol=symbol, action="BUY", price=0.0, timestamp=bars.index[-1], reason="test buy")


class _NeverBuy(Strategy):
    min_history = 5

    def generate_signal(self, symbol, bars):
        return Signal(symbol=symbol, action="HOLD", price=0.0, timestamp=bars.index[-1], reason="nope")


def _bars(closes, volumes):
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="B")
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": volumes}, index=idx,
    )


def test_scan_computes_change_and_signals():
    constituents = pd.DataFrame({"symbol": ["AAA"], "name": ["Aaa Corp"], "sector": ["Tech"]})
    bars = {"AAA": _bars([100.0] * 29 + [110.0], [1000] * 29 + [5000])}
    result = scan(bars, constituents, {"buyer": _AlwaysBuy(), "holder": _NeverBuy()})

    row = result.iloc[0]
    assert row["change_pct"] == 10.0
    assert row["volume_ratio"] == 5.0
    assert row["buyer"] == "test buy"
    assert row["holder"] == ""
