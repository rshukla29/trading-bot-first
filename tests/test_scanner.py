from datetime import datetime

from tradingbot.ibkr.scanner import merge_scan_universe, should_scan_movers_today


def test_merge_scan_universe_combines_and_dedupes():
    result = merge_scan_universe(fixed=["XOM", "JNJ"], movers=["JNJ", "BRZE"], exclude=[])
    assert result == ["BRZE", "JNJ", "XOM"]


def test_merge_scan_universe_excludes_other_strategy_watchlist():
    result = merge_scan_universe(fixed=["XOM"], movers=["AAPL", "BRZE"], exclude=["AAPL", "MSFT"])
    assert result == ["BRZE", "XOM"]


def test_merge_scan_universe_empty_movers():
    result = merge_scan_universe(fixed=["XOM", "JNJ"], movers=[], exclude=[])
    assert result == ["JNJ", "XOM"]


def test_should_scan_movers_today_false_on_friday():
    # 2026-09-11 was the actual Friday that motivated this guard.
    assert should_scan_movers_today(datetime(2026, 9, 11)) is False


def test_should_scan_movers_today_true_on_weekdays():
    monday_through_thursday = [datetime(2026, 9, 7 + d) for d in range(4)]  # Mon-Thu
    assert all(should_scan_movers_today(d) for d in monday_through_thursday)
