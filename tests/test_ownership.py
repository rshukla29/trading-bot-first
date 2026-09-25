import tempfile
from pathlib import Path

import pytest

from tradingbot.journal import Journal
from tradingbot.ownership import resolve_ownership


@pytest.fixture
def journal():
    with tempfile.TemporaryDirectory() as tmp:
        yield Journal(str(Path(tmp) / "test.db"))


def test_manual_position_is_never_mine(journal):
    # ASTX bought manually: held, but never claimed by the bot.
    own = resolve_ownership(journal, "ma_crossover", {"ASTX"}, set())
    assert own.mine == set()
    assert own.not_mine == {"ASTX"}


def test_claimed_and_held_is_mine(journal):
    journal.claim_stock("NVDA", "ma_crossover")
    own = resolve_ownership(journal, "ma_crossover", {"NVDA", "ASTX"}, set())
    assert own.mine == {"NVDA"}
    assert own.not_mine == {"ASTX"}


def test_other_strategy_position_is_not_mine(journal):
    journal.claim_stock("BAC", "rsi2")
    assert resolve_ownership(journal, "ma_crossover", {"BAC"}, set()).not_mine == {"BAC"}
    assert resolve_ownership(journal, "rsi2", {"BAC"}, set()).mine == {"BAC"}


def test_stale_claim_released_once_flat_with_no_orders(journal):
    journal.claim_stock("PFE", "rsi2")  # stopped out / sold
    resolve_ownership(journal, "rsi2", set(), set())
    assert journal.owned_stocks("rsi2") == set()


def test_pending_buy_keeps_claim(journal):
    # BUY placed after the close: not held yet, but its order is still working.
    journal.claim_stock("XLF", "rsi2")
    own = resolve_ownership(journal, "rsi2", set(), {"XLF"})
    assert journal.owned_stocks("rsi2") == {"XLF"}
    assert own.mine == set()


def test_manual_shares_on_same_symbol_later_bought_by_hand_stay_unmanaged(journal):
    # Claim released after a stop-out; a later manual buy of the same
    # symbol must not be picked back up by the bot.
    journal.claim_stock("TSLA", "ma_crossover")
    resolve_ownership(journal, "ma_crossover", set(), set())
    assert resolve_ownership(journal, "ma_crossover", {"TSLA"}, set()).not_mine == {"TSLA"}
