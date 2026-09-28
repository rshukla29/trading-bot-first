from datetime import date

from tradingbot.run_snapshot import _is_friday, _is_last_trading_day_of_month


def test_is_friday_true_on_friday():
    assert _is_friday(date(2026, 9, 25)) is True  # a Friday


def test_is_friday_false_on_other_weekdays():
    assert _is_friday(date(2026, 9, 22)) is False  # Tuesday
    assert _is_friday(date(2026, 9, 24)) is False  # Thursday


def test_last_trading_day_when_month_ends_on_a_weekday():
    # 2026-09-30 is a Wednesday -- the last trading day is the last day itself.
    assert _is_last_trading_day_of_month(date(2026, 9, 30)) is True
    assert _is_last_trading_day_of_month(date(2026, 9, 29)) is False


def test_last_trading_day_rolls_back_from_a_weekend():
    # 2026-08-31 is a Monday, so August's last CALENDAR day (the 31st, a
    # Monday) is the same as the last weekday -- pick a month that
    # actually ends on a weekend instead: 2027-01-31 is a Sunday, so the
    # last trading day should be Friday 2027-01-29.
    assert _is_last_trading_day_of_month(date(2027, 1, 31)) is False  # Sunday
    assert _is_last_trading_day_of_month(date(2027, 1, 30)) is False  # Saturday
    assert _is_last_trading_day_of_month(date(2027, 1, 29)) is True  # Friday, the real last trading day


def test_last_trading_day_false_mid_month():
    assert _is_last_trading_day_of_month(date(2026, 9, 15)) is False


def test_report_dir_is_year_month_day_tree():
    from pathlib import Path

    from tradingbot.run_snapshot import report_dir

    base = Path("reports")
    assert report_dir(base, date(2026, 9, 8), per_day=True) == Path("reports/2026/09/08")
    assert report_dir(base, date(2026, 9, 25), per_day=False) == Path("reports/2026/09")


def test_dated_dir_shared_by_reports_and_scans():
    from pathlib import Path

    from tradingbot.paths import dated_dir

    assert dated_dir(Path("scans"), date(2026, 9, 25)) == Path("scans/2026/09/25")
    assert dated_dir(Path("scans"), date(2026, 9, 25), per_day=False) == Path("scans/2026/09")
