import tempfile
from datetime import time
from pathlib import Path

import pandas as pd
import pytest

from tradingbot.journal import Journal
from tradingbot.options.credit_orb import (
    CreditOrbConfig, backtest_credit_orb, contracts_for, find_breakout, spread_strikes, spread_value,
)
from tradingbot.position_guard import intended_short_conids

ET = "America/New_York"


def _day(closes, start="2026-10-02 09:30", spread=0.3):
    idx = pd.date_range(start, periods=len(closes), freq="5min", tz=ET)
    return pd.DataFrame({"open": closes, "high": [c + spread for c in closes],
                         "low": [c - spread for c in closes], "close": closes, "volume": 1e6}, index=idx)


RANGE_12 = [700.0, 701.0, 699.5, 700.5, 700.0, 701.5, 699.0, 700.0, 700.5, 701.0, 700.0, 699.5]  # 9:30-10:25


def test_breakout_up_after_the_60m_range():
    sig = find_breakout(_day(RANGE_12 + [700.5, 703.0]), CreditOrbConfig())
    assert sig.direction == "up" and sig.bar_ts.strftime("%H:%M") == "10:35"
    assert sig.range_low == pytest.approx(698.7) and sig.range_high == pytest.approx(701.8)


def test_no_breakout_inside_the_range_or_after_noon():
    assert find_breakout(_day(RANGE_12 + [700.0] * 10), CreditOrbConfig()) is None
    late = _day(RANGE_12 + [700.0] * 18 + [710.0])  # first close outside at 12:00 -> closes 12:05
    assert find_breakout(late, CreditOrbConfig()) is None


def test_min_range_filter():
    flat = _day([700.0] * 12 + [702.0], spread=0.1)  # range 0.2 on 700 = 0.03% < 0.2%
    assert find_breakout(flat, CreditOrbConfig()) is None


def test_spread_strikes_sit_on_the_far_side_of_the_range():
    up = find_breakout(_day(RANGE_12 + [703.0]), CreditOrbConfig())
    assert spread_strikes(up, CreditOrbConfig()) == ("P", 698.0, 696.0)   # short put at/below range low 698.7
    down = find_breakout(_day(RANGE_12 + [697.0]), CreditOrbConfig())
    assert spread_strikes(down, CreditOrbConfig()) == ("C", 702.0, 704.0)  # short call at/above range high 701.8


def test_contracts_capped_by_max_loss_and_max_contracts():
    cfg = CreditOrbConfig(width=2.0, max_loss_dollars=500, max_contracts=10)
    assert contracts_for(0.30, cfg) == 2     # max loss $170 per contract -> 2
    assert contracts_for(2.10, cfg) == 0     # credit >= width: nonsense quote, no trade
    assert contracts_for(1.90, CreditOrbConfig(max_loss_dollars=5000)) == 10


def test_spread_value_bounds():
    v = spread_value(700.0, "P", 698.0, 696.0, 60, 0.18, 0.04)
    assert 0 < v < 2.0
    assert spread_value(800.0, "P", 698.0, 696.0, 1, 0.18, 0.04) == pytest.approx(0.0, abs=1e-6)


def test_backtest_keeps_credit_when_price_holds():
    bars = _day(RANGE_12 + [703.0] + [704.0] * 66)  # breakout up, then drifts higher to the close
    vol = pd.Series(0.45, index=bars.index)  # enough premium for a credit above the minimum
    trades = backtest_credit_orb(bars, vol, CreditOrbConfig())
    assert len(trades) == 1 and trades[0].exit_reason == "close" and trades[0].pnl > 0


@pytest.fixture
def journal():
    with tempfile.TemporaryDirectory() as tmp:
        yield Journal(str(Path(tmp) / "t.db"))


def test_pending_credit_spread_short_leg_is_an_intended_short(journal):
    pid = journal.open_credit_orb("SPY", "2026-10-02", "up", "P", 698, 696, 111, 222, 2, 0.3, 698.7, 701.8)
    assert 111 in intended_short_conids(journal)       # pending: protected before the order can fill
    journal.confirm_credit_orb(pid, 2, 0.31)
    assert 111 in intended_short_conids(journal)
    journal.close_credit_orb(pid, 0.05, "close_1550", 52.0)
    assert 111 not in intended_short_conids(journal)
    assert journal.credit_orb_traded_on("2026-10-02")


def test_credit_limit_ladder_steps_from_mid_to_natural():
    from tradingbot.options.credit_orb import credit_limit_ladder
    # short 0.20/0.26 (mid .23), long 0.08/0.12 (mid .10): mid .13, natural .08
    assert credit_limit_ladder(0.20, 0.26, 0.08, 0.12, floor=0.05) == [0.13, 0.11, 0.08]
    assert credit_limit_ladder(0.20, 0.26, 0.08, 0.12, floor=0.10) == [0.13, 0.11]   # natural below the floor
    assert credit_limit_ladder(0.20, 0.20, 0.10, 0.10, floor=0.05) == [0.10]          # tight market: one price
    assert credit_limit_ladder(0.05, 0.07, 0.03, 0.05, floor=0.05) == []              # nothing worth selling
