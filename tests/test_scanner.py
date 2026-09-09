from tradingbot.ibkr.scanner import merge_scan_universe


def test_merge_scan_universe_combines_and_dedupes():
    result = merge_scan_universe(fixed=["XOM", "JNJ"], movers=["JNJ", "BRZE"], exclude=[])
    assert result == ["BRZE", "JNJ", "XOM"]


def test_merge_scan_universe_excludes_other_strategy_watchlist():
    result = merge_scan_universe(fixed=["XOM"], movers=["AAPL", "BRZE"], exclude=["AAPL", "MSFT"])
    assert result == ["BRZE", "XOM"]


def test_merge_scan_universe_empty_movers():
    result = merge_scan_universe(fixed=["XOM", "JNJ"], movers=[], exclude=[])
    assert result == ["JNJ", "XOM"]
