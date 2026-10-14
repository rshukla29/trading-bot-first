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


def test_trend_join_position_lifecycle(journal):
    pid = journal.open_trend_join_position("AAA", "2026-09-25", 106.0, 18, 101.97, 555, "gap 4%")
    assert [r["symbol"] for r in journal.open_trend_join_positions()] == ["AAA"]
    assert journal.trend_join_symbols_traded_on("2026-09-25") == ["AAA"]

    journal.update_trend_join_position(pid, remaining=12, partial_taken=1, realized_pnl=15.0)
    row = journal.open_trend_join_positions()[0]
    assert row["remaining"] == 12 and row["partial_taken"] == 1

    journal.close_trend_join_position(pid, "trailing_stop", 40.0)
    assert journal.open_trend_join_positions() == []
    # Still counts toward the day's trades after closing (no same-day re-entry).
    assert journal.trend_join_symbols_traded_on("2026-09-25") == ["AAA"]


def test_update_trend_join_position_rejects_unknown_fields(journal):
    pid = journal.open_trend_join_position("AAA", "2026-09-25", 106.0, 18, 101.97, None, "x")
    with pytest.raises(ValueError):
        journal.update_trend_join_position(pid, status="closed")


# --- bought/sold prices backed out of IBKR's realized P&L ----------------------

from tradingbot.journal import closed_trade_multiplier, closed_trade_prices  # noqa: E402


def test_multiplier_from_closed_trade_symbol():
    assert closed_trade_multiplier("SPY 20261013 777.0P") == 100.0
    assert closed_trade_multiplier("JNJ") == 1.0


def test_long_option_entry_price_from_realized_pnl():
    # 2026-10-13: bought 10 x 777P at 0.79, sold at 0.91; IBKR realized
    # 110.85 after ~$6.6 opening + ~$2.6 closing commission.
    bought, sold = closed_trade_prices("SLD", 10, 0.91, 110.85, 2.6, 100.0)
    assert sold == 0.91 and round(bought, 2) == 0.80   # 0.79 + opening commission per share


def test_short_closed_by_a_buy_shows_the_credit_as_sold():
    # Sold 2 calls at 0.12, bought back at 0.08: +$8 less $1.3 commission each side.
    bought, sold = closed_trade_prices("BOT", 2, 0.08, 6.70, 1.3, 100.0)
    assert bought == 0.08 and round(sold, 2) == 0.12


def test_stock_entry_price():
    bought, sold = closed_trade_prices("SLD", 7, 257.20, 21.97, 1.0, 1.0)
    assert round(bought, 2) == 253.92 and sold == 257.20


def test_rerun_fills_in_a_missing_commission(tmp_path):
    journal = Journal(str(tmp_path / "j.db"))
    journal.record_closed_trade("e1", "2026-10-13", "SPY", "SLD", 10, 0.91, 110.85)
    journal.record_closed_trade("e1", "2026-10-13", "SPY", "SLD", 10, 0.91, 110.85, commission=2.6)
    (row,) = journal.closed_trades_between("2026-10-13", "2026-10-13")
    assert row["commission"] == 2.6
