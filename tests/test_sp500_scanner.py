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


# --- Yahoo batch download: retry symbols it silently drops -------------------

def _yahoo_frame(symbols):
    idx = pd.date_range("2026-10-01", periods=3, freq="B")
    cols = pd.MultiIndex.from_product([symbols, ["Open", "High", "Low", "Close", "Volume"]])
    return pd.DataFrame(1.0, index=idx, columns=cols)


def test_download_retries_symbols_yahoo_dropped(monkeypatch):
    import tradingbot.sp500_scanner as sc
    calls = []

    def fake_download(symbols, **kw):
        calls.append((list(symbols), kw["threads"]))
        if len(calls) == 1:   # threaded batch drops FAST and IQV ("possibly delisted")
            return _yahoo_frame([s for s in symbols if s not in ("FAST", "IQV")])
        return _yahoo_frame(list(symbols))

    monkeypatch.setattr(sc.yf, "download", fake_download)
    bars = sc.download_daily_bars(["AAPL", "FAST", "IQV", "MSFT"], retry_pause=0)
    assert sorted(bars) == ["AAPL", "FAST", "IQV", "MSFT"]
    assert calls[1] == (["FAST", "IQV"], False)   # only the missing ones, unthreaded
    assert len(calls) == 2


def test_download_gives_up_after_retries(monkeypatch):
    import tradingbot.sp500_scanner as sc
    calls = []

    def fake_download(symbols, **kw):
        calls.append(list(symbols))
        return _yahoo_frame([s for s in symbols if s != "GONE"])

    monkeypatch.setattr(sc.yf, "download", fake_download)
    bars = sc.download_daily_bars(["AAPL", "GONE"], retries=2, retry_pause=0)
    assert sorted(bars) == ["AAPL"]
    assert calls == [["AAPL", "GONE"], ["GONE"], ["GONE"]]
