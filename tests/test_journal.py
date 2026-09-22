import tempfile
from pathlib import Path

import pytest

from tradingbot.journal import Journal


@pytest.fixture
def journal():
    with tempfile.TemporaryDirectory() as tmp:
        yield Journal(str(Path(tmp) / "test.db"))


def test_balance_before_returns_none_when_nothing_recorded(journal):
    assert journal.balance_before("2026-09-14") is None


def test_record_and_read_balance_before(journal):
    journal.record_daily_balance("2026-09-11", 1_000_000.0)
    journal.record_daily_balance("2026-09-12", 1_000_500.0)
    # Weekly report starting Monday 2026-09-14 should baseline off the
    # most recent prior trading day (Friday the 12th), not the 11th.
    assert journal.balance_before("2026-09-14") == 1_000_500.0


def test_balance_before_is_strictly_before_not_on_or_before(journal):
    journal.record_daily_balance("2026-09-14", 1_000_000.0)
    assert journal.balance_before("2026-09-14") is None


def test_record_daily_balance_upserts_same_date(journal):
    journal.record_daily_balance("2026-09-14", 1_000_000.0)
    journal.record_daily_balance("2026-09-14", 1_001_234.0)  # rerun same day
    assert journal.balance_before("2026-09-15") == 1_001_234.0


def test_closed_trades_between_filters_by_date_range(journal):
    journal.record_closed_trade("e1", "2026-09-09", "BRZE", "SLD", 3943, 24.65, -582.0)
    journal.record_closed_trade("e2", "2026-09-21", "SPY", "SLD", 10, 1.27, 418.06)
    journal.record_closed_trade("e3", "2026-09-25", "AAPL", "SLD", 5, 200.0, 12.0)

    week = journal.closed_trades_between("2026-09-21", "2026-09-21")
    assert [r["exec_id"] for r in week] == ["e2"]

    month = journal.closed_trades_between("2026-09-01", "2026-09-30")
    assert [r["exec_id"] for r in month] == ["e1", "e2", "e3"]


def test_record_closed_trade_dedupes_by_exec_id(journal):
    journal.record_closed_trade("e1", "2026-09-21", "SPY", "SLD", 10, 1.27, 418.06)
    journal.record_closed_trade("e1", "2026-09-21", "SPY", "SLD", 10, 1.27, 418.06)  # rerun same snapshot
    rows = journal.closed_trades_between("2026-09-21", "2026-09-21")
    assert len(rows) == 1
