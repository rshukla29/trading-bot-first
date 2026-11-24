"""ORB-0DTE-HP: the entry and exit rules (options/orb_hp_strategy.py) and the
bot's signal handling (run_orb_hp_bot.py), IBKR faked."""
from datetime import date, datetime, time
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd
import pytest

from tradingbot.journal import Journal
from tradingbot.options import orb_hp_strategy as s

P = s.HpParams()
COLS = ["open", "close", "ema9", "sma50", "sma200", "vwap", "macd", "signal"]


def frame(prev, now):
    return pd.DataFrame([prev, now], columns=COLS)


CALL_PREV = dict(open=600, close=600.5, ema9=600, sma50=599, sma200=598, vwap=599.5, macd=0.10, signal=0.12)
CALL_NOW = dict(open=600.5, close=601.5, ema9=600.6, sma50=599.1, sma200=598.1, vwap=599.7, macd=0.20, signal=0.14)


def flip(row):
    """The put mirror of a call row, around 600 / MACD 0."""
    m = {k: 1200 - v for k, v in row.items() if k not in ("macd", "signal")}
    return {**m, "macd": -row["macd"], "signal": -row["signal"]}


# ---------------------------------------------------------------- entries

def test_call_when_everything_lines_up():
    assert s.signal_at(frame(CALL_PREV, CALL_NOW), 1) == s.CALL


def test_put_is_the_mirror_image():
    assert s.signal_at(frame(flip(CALL_PREV), flip(CALL_NOW)), 1) == s.PUT


@pytest.mark.parametrize("change", [
    dict(close=600.5),             # not above the 9 EMA (600.6)
    dict(sma50=602),               # not above the 50 SMA
    dict(sma200=602),              # not above the 200 SMA
    dict(vwap=602),                # not above VWAP
    dict(macd=0.13),               # MACD below its signal line: no cross
])
def test_each_call_condition_is_required(change):
    assert s.signal_at(frame(CALL_PREV, {**CALL_NOW, **change}), 1) is None


def test_macd_must_cross_on_this_candle_and_be_above_zero():
    already_above = {**CALL_PREV, "macd": 0.15, "signal": 0.12}
    assert s.signal_at(frame(already_above, CALL_NOW), 1) is None
    below_zero = frame({**CALL_PREV, "macd": -0.30, "signal": -0.20}, {**CALL_NOW, "macd": -0.05, "signal": -0.10})
    assert s.signal_at(below_zero, 1) is None


def test_no_signal_before_the_200_sma_exists():
    bars = pd.DataFrame({"open": [1.0] * 150, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
                        index=pd.date_range("2026-11-19 09:30", periods=150, freq="5min", tz="America/New_York"))
    assert s.latest_signal(bars, P) == (None, None)


def test_real_series_gives_both_sides():
    rng = np.random.default_rng(3)
    idx = pd.date_range("2026-01-05 09:30", periods=78 * 30, freq="5min", tz="America/New_York")
    close = 500 + np.cumsum(rng.normal(0, 0.3, len(idx)))
    bars = pd.DataFrame({"open": close - rng.normal(0, 0.1, len(idx)), "high": close + 0.2, "low": close - 0.2,
                         "close": close, "volume": 1000.0}, index=idx)
    ind = s.indicators(bars, P)
    sigs = [s.signal_at(ind, i, P) for i in range(len(ind))]
    assert sigs.count(s.CALL) > 0 and sigs.count(s.PUT) > 0


# ---------------------------------------------------------------- exits

def test_candle_colour_exits():
    assert s.candle_exit(s.CALL, 600, 599.9) and not s.candle_exit(s.CALL, 600, 600.1)
    assert s.candle_exit(s.PUT, 600, 600.1) and not s.candle_exit(s.PUT, 600, 599.9)
    assert not s.candle_exit(s.CALL, 600, 600) and not s.candle_exit(s.PUT, 600, 600)   # doji: hold


def test_exit_priority():
    kw = dict(eod=time(15, 55), stop_fraction=None, ladder_stop=None, target_fraction=None, candle=True)
    assert s.exit_reason(time(15, 55), 1, 1, **kw) == "eod_flatten"
    assert s.exit_reason(time(11, 0), 1, 1, **kw) == "candle_exit"
    assert s.exit_reason(time(11, 0), 0.6, 1, **{**kw, "stop_fraction": 0.3}) == "stop_loss"
    assert s.exit_reason(time(11, 0), 1, 1, **{**kw, "candle": False}) is None


# ---------------------------------------------------------------- journal, reports, rules

def test_own_table_and_reports(tmp_path):
    from tradingbot import dashboard, strategy_pnl
    import sqlite3
    db = str(tmp_path / "j.db")
    j = Journal(db)
    pid = j.open_orb_position("SPY", "2026-11-19T15:00:00+00:00", "20261119", 600, "P", 7, 1.0, 2, 599, 598,
                              table="orb_hp_positions")
    assert j.open_orb_position_row("SPY") is None
    j.close_orb_position(pid, "2026-11-19T15:10:00+00:00", 1.2, "candle_exit", 40.0, table="orb_hp_positions")
    df = strategy_pnl.strategy_pnl(db, date(2026, 11, 19), date(2026, 11, 19)).set_index("strategy")
    assert df.loc["ORB-0DTE-HP (SPY 0DTE options)", "realized"] == 40.0
    assert df.loc["ORB (SPY 0DTE options)", "realized"] == 0.0
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    assert any(t["strategy"].startswith("ORB-0DTE-HP") for t in dashboard.closed_trades(conn))


def test_expiry_guard_books_hp_positions(tmp_path):
    from tradingbot import run_expiry_guard as g
    j = Journal(str(tmp_path / "j.db"))
    j.open_orb_position("SPY", "2026-11-19T15:00:00+00:00", "20261119", 600, "C", 5, 1.0, 1, 599, 598,
                        table="orb_hp_positions")
    g._close_journal_rows(j, 5, 0.1)
    assert j.open_orb_position_row("SPY", table="orb_hp_positions") is None


def test_rules_card_defaults():
    from tradingbot import rules
    r = rules.load("orb_hp")
    assert r.stop_fraction(1.0, 100) is None and r.profit_target() is None and r.ladder_stop(1, 3, 100) is None
    assert r["maxQty"] == 2 and r["trades"] == 1
    assert list(rules.SCHEMA)[:2] == ["orb", "orb_hp"]


# ---------------------------------------------------------------- the bot

@pytest.fixture
def bot(monkeypatch):
    from tradingbot import run_orb_hp_bot as b
    monkeypatch.setattr(b.GATE, "entries_allowed", lambda: True)
    return b


def _bars(open_=600.0, close=601.0, minute=0):
    idx = pd.date_range(f"2026-11-19 10:{minute:02d}", periods=1, freq="5min", tz="America/New_York")
    return pd.DataFrame({"open": [open_], "close": [close]}, index=idx)


ROW = pd.Series({"close": 601.0, "ema9": 600.0, "sma50": 599.0, "sma200": 598.0, "vwap": 599.5,
                 "macd": 0.2, "signal": 0.1})


def _state(call=None, put=None):
    return {"symbol": "SPY", "positions": {"C": call, "P": put}, "pending": {"C": None, "P": None}}


def _new_bar(bot, monkeypatch, sig, state=None, sides="CP", exit_mode="eod", **bar):
    monkeypatch.setattr(bot.settings, "orb_hp_sides", sides)
    monkeypatch.setattr(bot.settings, "orb_hp_exit", exit_mode)
    monkeypatch.setattr(bot, "latest_signal", lambda bars, p: (sig, ROW))
    state = state or _state()
    bot.on_new_bar(NS(log_signal=lambda *a, **k: None), state, _bars(**bar))
    return state


def test_live_defaults_are_one_trade_a_day_before_11_held_to_the_close():
    from tradingbot import rules
    from tradingbot.config import settings
    assert settings.orb_hp_sides == "CP" and settings.orb_hp_exit == "eod"
    assert settings.orb_hp_last_entry_time == "11:00" and rules.load("orb_hp")["trades"] == 1


def test_signal_arms_its_side(bot, monkeypatch):
    assert _new_bar(bot, monkeypatch, s.CALL)["pending"]["C"]["right"] == "C"
    assert _new_bar(bot, monkeypatch, s.PUT)["pending"]["P"]["right"] == "P"
    assert _new_bar(bot, monkeypatch, s.PUT, sides="C")["pending"]["P"] is None


def test_never_a_call_and_a_put_together(bot, monkeypatch):
    st = _new_bar(bot, monkeypatch, s.PUT, state=_state(call={"id": 1, "right": "C"}))
    assert st["pending"]["P"] is None
    st = _new_bar(bot, monkeypatch, s.CALL, state=_state(put={"id": 1, "right": "P"}))
    assert st["pending"]["C"] is None


def test_held_to_the_close_unless_candle_exit_is_chosen(bot, monkeypatch):
    st = _new_bar(bot, monkeypatch, None, state=_state(call={"id": 1, "right": "C"}), open_=601, close=600)
    assert not st["positions"]["C"].get("candle_exit")   # default: a red candle doesn't sell
    st = _new_bar(bot, monkeypatch, None, state=_state(call={"id": 1, "right": "C"}, put={"id": 2, "right": "P"}),
                  exit_mode="candle", open_=601, close=600)
    assert st["positions"]["C"]["candle_exit"] and not st["positions"]["P"].get("candle_exit")


def test_pending_lapses_after_one_candle(bot, monkeypatch):
    st = _new_bar(bot, monkeypatch, s.CALL)
    monkeypatch.setattr(bot, "latest_signal", lambda bars, p: (None, None))
    bot.on_new_bar(NS(log_signal=lambda *a, **k: None), st, _bars(minute=5))
    assert st["pending"]["C"] is None


def _tick(bot, monkeypatch, now, entries=None, positions=None, pending=("P",)):
    monkeypatch.setattr(bot, "_now_et", lambda: now.replace(tzinfo=bot.ET))
    monkeypatch.setattr(bot, "_price", lambda ib, c: 601.0)
    monkeypatch.setattr(bot, "check_exit", lambda *a, **k: False)
    entered = []
    monkeypatch.setattr(bot, "enter", lambda ib, j, st, pend, sp, eq: entered.append(pend["right"]) or {"id": 3})
    bar = pd.Timestamp("2026-11-19 10:55", tz="America/New_York")
    state = {"symbol": "SPY", "underlying": None, "positions": positions or {"C": None, "P": None},
             "entries_today": entries or {"C": 0, "P": 0},
             "fetched_boundary": pd.Timestamp(now.replace(tzinfo=bot.ET)).floor("5min"),
             "pending": {r: ({"bar_ts": bar, "right": r, "sma50": 599.0, "sma200": 598.0} if r in pending else None)
                         for r in ("C", "P")}}
    bot.tick(None, NS(net_liquidation=lambda: 1e6), None, state)
    return state, entered


def test_tick_enters_before_11_only_and_once_a_day(bot, monkeypatch):
    st, entered = _tick(bot, monkeypatch, datetime(2026, 11, 19, 10, 40, 20))
    assert entered == ["P"] and st["entries_today"]["P"] == 1 and st["positions"]["P"] == {"id": 3}
    st, entered = _tick(bot, monkeypatch, datetime(2026, 11, 19, 11, 5, 20))
    assert entered == [] and st["pending"]["P"] is None          # after 11am: dropped
    st, entered = _tick(bot, monkeypatch, datetime(2026, 11, 19, 10, 40, 20), entries={"C": 1, "P": 0})
    assert entered == []                                          # today's one trade (a call) already taken
    st, entered = _tick(bot, monkeypatch, datetime(2026, 11, 19, 10, 40, 20),
                        positions={"C": {"id": 9, "right": "C"}, "P": None}, pending=("P",))
    assert entered == []                                          # a call is open: no put alongside it
    st, entered = _tick(bot, monkeypatch, datetime(2026, 11, 19, 10, 40, 20), pending=("C", "P"))
    assert entered == ["C"] and st["pending"]["P"] is None        # both armed at once: only one is bought


def test_journal_counts_and_recovers_each_side(tmp_path):
    j = Journal(str(tmp_path / "j.db"))
    for right in ("C", "P"):
        j.open_orb_position("SPY", "2026-11-19T15:00:00+00:00", "20261119", 600, right, 1, 1.0, 1, 599, 598,
                            table="orb_hp_positions")
    assert j.option_entries_on("orb_hp_positions", "SPY", "2026-11-19", right="C") == 1
    assert j.option_entries_on("orb_hp_positions", "SPY", "2026-11-19") == 2
    assert [r["right"] for r in j.open_option_rows("orb_hp_positions", "SPY")] == ["C", "P"]


def test_task_schedule_and_controls():
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    plan = json.loads((root / "scripts" / "schedule.json").read_text(encoding="utf-8"))["tasks"]
    t = next(t for t in plan if t["name"] == "OrbHpTradingBot")
    assert t["ny"] == "09:30" and (root / "scripts" / t["script"]).exists()
    from tradingbot import control
    assert control.STRATEGIES["orb_hp"]["task"] == "OrbHpTradingBot"


# ---------------------------------------------------------------- SPY and QQQ

class _Client:
    ib = NS(sleep=lambda s: None)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _clock(bot, monkeypatch, open_calls=2):
    """Market hours for the first few clock reads, then after the close (one loop pass)."""
    calls = {"n": 0}

    def now():
        calls["n"] += 1
        t = time(10, 0) if calls["n"] <= open_calls else time(16, 30)
        return datetime(2026, 11, 24, t.hour, t.minute, tzinfo=bot.ET)
    monkeypatch.setattr(bot, "_now_et", now)


def test_defaults_trade_spy_and_qqq():
    from tradingbot.config import Settings
    fields = Settings.model_fields   # the code's defaults (a .env can still narrow them)
    assert [fields[k].default for k in ("orb_symbols", "orb_hp_symbols", "credit_orb_symbols")] == ["SPY,QQQ"] * 3
    assert Settings(_env_file=None, orb_hp_symbols=" spy, qqq ,").orb_hp_symbol_list == ["SPY", "QQQ"]


def test_main_runs_each_symbol_on_its_own(bot, monkeypatch):
    monkeypatch.setattr(bot.settings, "orb_hp_symbols", "SPY, qqq")
    monkeypatch.setattr(bot, "IBKRClient", lambda *a, **k: _Client())
    monkeypatch.setattr(bot, "Journal", lambda path: None)
    monkeypatch.setattr(bot.GATE, "stopped", lambda: False)
    monkeypatch.setattr(bot, "new_state", lambda ib, j, sym, today: {"symbol": sym, "positions": {"C": None, "P": None}})
    ticks = []
    monkeypatch.setattr(bot, "tick", lambda ib, c, j, st: ticks.append(st["symbol"]))
    _clock(bot, monkeypatch)
    bot.main()
    assert ticks == ["SPY", "QQQ"]


def test_a_symbol_that_fails_to_set_up_doesnt_stop_the_other(bot, monkeypatch):
    monkeypatch.setattr(bot.settings, "orb_hp_symbols", "SPY,QQQ")
    monkeypatch.setattr(bot, "IBKRClient", lambda *a, **k: _Client())
    monkeypatch.setattr(bot, "Journal", lambda path: None)
    monkeypatch.setattr(bot.GATE, "stopped", lambda: False)

    def new_state(ib, j, sym, today):
        if sym == "QQQ":
            raise RuntimeError("no market data")
        return {"symbol": sym, "positions": {"C": None, "P": None}}
    monkeypatch.setattr(bot, "new_state", new_state)
    ticks = []
    monkeypatch.setattr(bot, "tick", lambda ib, c, j, st: ticks.append(st["symbol"]))
    _clock(bot, monkeypatch)
    bot.main()
    assert ticks == ["SPY"]


def test_each_symbol_has_its_own_trade_count(tmp_path):
    j = Journal(str(tmp_path / "j.db"))
    j.open_orb_position("SPY", "2026-11-24T15:00:00+00:00", "20261124", 700, "P", 1, 1.0, 1, 699, 698,
                        table="orb_hp_positions")
    assert j.option_entries_on("orb_hp_positions", "SPY", "2026-11-24") == 1
    assert j.option_entries_on("orb_hp_positions", "QQQ", "2026-11-24") == 0   # QQQ can still trade today
    assert j.open_option_rows("orb_hp_positions", "QQQ") == []
