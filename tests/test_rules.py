"""Rules tab (rules.py): defaults equal today's settings, saving and checks,
what the bots read, and the control server's /api/rules."""
import json
from types import SimpleNamespace as NS

import pytest

from tradingbot import rules
from tradingbot.config import settings
from tradingbot.risk import ladder_stop, position_size


def _vals():
    return rules.effective({})[1]


def _save(changes: dict, capital=None, account=1_000_000.0):
    vals = rules.effective()[1]  # what's saved now, plus the changes
    for sid, fields in changes.items():
        vals[sid].update(fields)
    return rules.save({"capital": capital, "strategies": vals}, account)


# ---------------------------------------------------------------- defaults

def test_no_file_means_todays_settings():
    cap, vals, problems = rules.effective()
    assert cap is None and problems == []
    assert vals["orb"]["maxQty"] == settings.options_max_contracts_per_trade
    assert vals["rsi2"]["stop"] == {"mode": "pct", "pct": settings.rsi2_stop_loss_pct * 100, "usd": 5.0}
    assert vals["ma_crossover"]["stop"]["pct"] == settings.stop_loss_pct * 100
    assert vals["stock_orb"]["size"]["usd"] == settings.stock_orb_max_loss_per_trade


@pytest.mark.parametrize("sid,mult,entry,args,peaks", [
    ("orb", 100, 1.0, (0.5, 0.25, 0.25), [1.0, 1.49, 1.5, 1.74, 1.75, 2.0, 2.6]),
    ("rsi2", 1, 100.0, (0.3, 0.15, 0.15), [100, 129, 130, 144.9, 145, 160, 199]),
])
def test_default_step_stop_matches_the_old_ladder(monkeypatch, sid, mult, entry, args, peaks):
    monkeypatch.setattr(settings, "options_ladder_trigger_pct", 0.5)
    monkeypatch.setattr(settings, "options_ladder_step_pct", 0.25)
    monkeypatch.setattr(settings, "options_ladder_trail_pct", 0.25)
    r = rules.load(sid)
    assert r["ladder"]["lock"] == args[0] * 100 - args[2] * 100 + (0 if sid == "orb" else 0)
    for peak in peaks:
        assert r.ladder_stop(entry, peak, mult) == ladder_stop(entry, peak, *args), peak


def test_default_stock_size_matches_position_size():
    r = rules.load("rsi2")
    for price in (12.5, 60.0, 480.0):
        assert r.stock_quantity(100_000, price) == position_size(
            100_000, price, settings.risk_pct_per_trade, settings.rsi2_stop_loss_pct, settings.max_position_value)


# ---------------------------------------------------------------- saving

def test_save_keeps_only_changes_and_logs_them():
    data, lines = _save({"orb": {"maxQty": 3, "maxPrem": {"mode": "usd", "usd": 2.5, "pct": 1.0}}}, capital=25_000)
    assert data["capital"] == 25_000
    assert data["strategies"] == {"orb": {"maxQty": 3, "maxPrem": {"mode": "usd", "usd": 2.5, "pct": 1.0}}}
    saved = json.loads(rules.path().read_text())
    assert saved["strategies"] == data["strategies"]
    assert lines[0] == "Capital: whole account → $25,000"
    assert any(line.startswith("ORB max contracts:") and line.endswith("→ 3 contracts") for line in lines)
    assert [e["text"] for e in rules.read_log()] == lines          # a save's lines in order
    r = rules.load("orb")
    assert r["maxQty"] == 3 and r.capital_setting == 25_000
    assert _save({})[1] == ["Capital: $25,000 → whole account"]
    assert rules.read_log()[0]["text"] == "Capital: $25,000 → whole account"   # newest save first


@pytest.mark.parametrize("sid,fields,key,words", [
    ("orb", {"size": {"mode": "pct", "pct": 8, "usd": 1, "fixed": 1}}, "size", "at most 5%"),
    ("orb", {"size": {"mode": "usd", "pct": 1, "usd": 2000, "fixed": 1}}, "size", "at most $1,250"),
    ("orb", {"maxQty": 51}, "maxQty", "1 to 50"),
    ("orb", {"ladder": {"on": True, "mode": "pct", "trig": 30, "lock": 30, "step": 10}}, "ladder", "lock in less"),
    ("rsi2", {"stop": {"mode": "pct", "pct": 100, "usd": 5}}, "stop", "less than 100%"),
    ("credit_orb", {"minCredit": 2.0, "width": 2.0}, "minCredit", "smaller than the spread width"),
    ("iron_condor", {"target": {"mode": "pct", "pct": 100}}, "target", "less than 100%"),
    ("trend_join", {"trades": 2.5}, "trades", "whole number"),
])
def test_bad_values_are_refused_with_the_reason(sid, fields, key, words):
    with pytest.raises(ValueError) as exc:
        _save({sid: fields}, capital=25_000)
    problems = json.loads(str(exc.value))["problems"]
    assert words in problems[sid][key]
    assert not rules.path().exists()                      # nothing saved


def test_capital_must_be_positive():
    with pytest.raises(ValueError) as exc:
        _save({}, capital=-5)
    assert "_capital" in json.loads(str(exc.value))["problems"]


def test_a_bad_saved_value_falls_back_to_its_default(caplog):
    rules.path().write_text(json.dumps({"strategies": {"orb": {"maxQty": 500, "trades": 1}}}))
    r = rules.load("orb")
    assert r["maxQty"] == settings.options_max_contracts_per_trade and r["trades"] == 1
    assert "ORB maxQty" in caplog.text


def test_a_broken_file_means_default_rules(caplog):
    rules.path().write_text("{not json")
    r = rules.load("orb")
    assert r.values == _vals()["orb"] and "can't be read" in caplog.text
    assert "can't be read" in rules.payload()["problems"][0]


# ---------------------------------------------------------------- what the bots use

def test_capital_is_never_more_than_the_account():
    r = rules.StrategyRules("orb", _vals()["orb"], 25_000)
    assert r.capital(1_000_000) == 25_000 and r.capital(10_000) == 10_000
    assert rules.StrategyRules("orb", _vals()["orb"], None).capital(80_000) == 80_000


def test_size_modes_and_caps():
    v = _vals()["orb"]
    r = rules.StrategyRules("orb", v, 25_000)
    assert r.quantity(25_000, 140.0) == 1                          # 1% = $250 -> one $140 contract
    v["size"] = {"mode": "usd", "pct": 1, "usd": 700.0, "fixed": 1}
    assert r.quantity(25_000, 140.0) == 5
    v["size"] = {"mode": "fixed", "pct": 1, "usd": 1, "fixed": 4}
    assert r.quantity(25_000, 999.0) == 4 and r.quantity(25_000, 999.0, cap=2) == 2


def test_option_rules_in_dollars():
    v = _vals()["orb"]
    v["maxPrem"] = {"mode": "usd", "usd": 1.50, "pct": 1}
    v["stop"] = {"mode": "usd", "pct": 30, "usd": 40.0}
    v["ladder"] = {"on": True, "mode": "usd", "trig": 50.0, "lock": 20.0, "step": 25.0}
    r = rules.StrategyRules("orb", v, None)
    assert r.max_premium(25_000) == 150.0
    assert r.stop_fraction(2.00, 100) == pytest.approx(0.2)        # $40 of a $200 contract
    assert r.ladder_stop(2.00, 2.49, 100) is None                  # up $49 a contract
    assert r.ladder_stop(2.00, 2.50, 100) == 2.20                  # up $50: lock in $20
    assert r.ladder_stop(2.00, 2.75, 100) == 2.45                  # each further $25: +$25


def test_orb_trades_per_day_and_daily_limit(monkeypatch):
    import tradingbot.run_orb_bot as orb
    _save({"orb": {"trades": 1, "daily": {"mode": "usd", "usd": 300.0, "pct": 2.0}}})
    state = {"entries_today": {"C": 0, "P": 0}}
    monkeypatch.setattr(rules, "realized_today", lambda sid, **k: -100.0)
    assert orb.rules_allow_entry("SPY", state, 100_000)
    monkeypatch.setattr(rules, "realized_today", lambda sid, **k: -300.0)
    assert not orb.rules_allow_entry("SPY", state, 100_000)        # limit reached
    monkeypatch.setattr(rules, "realized_today", lambda sid, **k: 0.0)
    state["entries_today"]["P"] = 1
    assert not orb.rules_allow_entry("SPY", state, 100_000)        # one trade a day, done


def test_rsi2_and_ma_daily_limit_is_only_the_users_rule(monkeypatch):
    # No built-in % (MAX_DAILY_LOSS_PCT is ignored): off until set on the tab.
    monkeypatch.setattr(rules._settings, "max_daily_loss_pct", 0.03)
    monkeypatch.setattr(rules, "realized_today", lambda sid, **k: -50_000.0)
    for sid in ("rsi2", "ma_crossover"):
        r = rules.load(sid)
        assert r.daily_limit(100_000) is None
        assert rules.daily_limit_hit(r, 100_000) is None
    # Each strategy then uses its own saved value, $ or % of capital.
    _save({"rsi2": {"daily": {"mode": "usd", "usd": 400.0, "pct": 2.0}},
           "ma_crossover": {"daily": {"mode": "pct", "usd": 500.0, "pct": 1.5}}})
    assert rules.load("rsi2").daily_limit(100_000) == 400.0
    assert rules.load("ma_crossover").daily_limit(100_000) == 1_500.0
    monkeypatch.setattr(rules, "realized_today", lambda sid, **k: -450.0)
    assert rules.daily_limit_hit(rules.load("rsi2"), 100_000) == 400.0
    assert rules.daily_limit_hit(rules.load("ma_crossover"), 100_000) is None


def test_realized_today_reads_the_strategys_journal(tmp_path, monkeypatch):
    from tradingbot.journal import Journal
    db = str(tmp_path / "j.db")
    j = Journal(db)
    pid = j.open_orb_position("SPY", "2026-11-17T15:00:00+00:00", "20261117", 600, "C", 1, 1.0, 2, 599, 601)
    j.close_orb_position(pid, "2026-11-17T16:00:00+00:00", 0.4, "stop", -120.0)
    from datetime import date
    assert rules.realized_today("orb", day=date(2026, 11, 17), db_path=db) == -120.0
    assert rules.realized_today("credit_orb", day=date(2026, 11, 17), db_path=db) == 0.0


def test_stop_audit_groups_positions_by_their_own_stop(monkeypatch):
    from tradingbot import protection
    calls = []
    monkeypatch.setattr(protection, "ensure_protective_stops",
                        lambda ib, j, group, pct, src: calls.append((sorted(group), pct)) or set())
    positions = {"A": NS(avgCost=50.0), "B": NS(avgCost=100.0), "C": NS(avgCost=200.0)}
    r = rules.StrategyRules("rsi2", dict(_vals()["rsi2"], stop={"mode": "usd", "pct": 15, "usd": 10.0}), None)
    protection.ensure_stops_by_symbol(None, None, positions, r.stock_stop_pcts(positions), 0.15, "t")
    assert calls == [(["C"], 0.05), (["B"], 0.1), (["A"], 0.2)]    # $10 below each cost


def test_step_stop_job_uses_each_strategys_ladder(tmp_path, monkeypatch):
    from tradingbot import run_step_stop_bot as bot
    from tradingbot.journal import Journal
    j = Journal(str(tmp_path / "j.db"))
    j.claim_stock("V", "ma_crossover")
    j.claim_stock("ADM", "rsi2")
    _save({"rsi2": {"ladder": {"on": False, "mode": "pct", "trig": 30, "lock": 15, "step": 15}}})
    by = bot.rules_by_symbol(j)
    assert by["ADM"].ladder_stop(100, 150) is None                 # RSI(2): step stop off
    assert by["V"].ladder_stop(100, 150) == 130.0                  # MA crossover: default 30/15/15


# ---------------------------------------------------------------- dashboard and server

def test_dashboard_has_the_rules_tab():
    from tradingbot import dashboard
    html = dashboard.rules_panel_html()
    assert 'id="rl-root"' in html and "/*__RULES_DATA__*/" not in html
    data = json.loads(html.split('<script id="rl-data" type="application/json">')[1].split("</script>")[0])
    assert data["strategies"] == rules.STRATEGIES and "<" not in json.dumps(data["schema"])
    assert '["rules", "Rules"]' in dashboard.TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture
def server(monkeypatch, tmp_path):
    from tests.test_control import server as control_server
    yield from control_server.__wrapped__(monkeypatch, tmp_path)


def test_server_reads_and_saves_rules(server):
    req, _, _ = server
    status, p = req("GET", "/api/rules")
    assert status == 200 and p["values"]["orb"]["maxQty"] == settings.options_max_contracts_per_trade
    vals = p["values"]
    vals["iron_condor"]["maxOpen"] = 1
    status, p = req("POST", "/api/rules", {"capital": 50_000, "strategies": vals})
    assert status == 200 and "Saved 2 changes" in p["message"]
    assert p["capital"] == 50_000 and rules.load("iron_condor")["maxOpen"] == 1
    assert [e["text"] for e in p["log"]][0].startswith("Capital")


def test_server_refuses_bad_rules_with_each_problem(server):
    req, _, _ = server
    vals = req("GET", "/api/rules")[1]["values"]
    vals["orb"]["maxQty"] = 0
    status, body = req("POST", "/api/rules", {"capital": None, "strategies": vals})
    assert status == 400
    assert json.loads(body["error"])["problems"]["orb"]["maxQty"].startswith("Use a number from 1")
    status, _ = req("POST", "/api/rules", {"capital": None, "strategies": vals}, headers={"Origin": "https://evil.example"})
    assert status == 403 and not rules.path().exists()


# ---------------------------------------------------------------- readiness and daily review

def test_readiness_flags_unusable_rules():
    from tradingbot import readiness as rd
    assert rd.check_rules()["status"] == rd.PASS and "default rules" in rd.check_rules()["detail"]
    _save({"rsi2": {"maxOpen": 2}}, capital=30_000)
    c = rd.check_rules()
    assert c["status"] == rd.PASS and c["detail"] == "Capital $30,000; custom rules for RSI(2)"
    rules.path().write_text(json.dumps({"strategies": {"orb": {"maxQty": 0}}}))
    c = rd.check_rules()
    assert c["status"] == rd.FAIL and "ORB maxQty" in c["detail"] and "Rules tab" in c["fix"]


def test_order_preview_size_follows_the_largest_option_rule():
    from tradingbot import readiness as rd
    assert rd.order_size() == 10 and rd.order_check_name() == "Orders allowed (10 contracts)"
    _save({"iron_condor": {"maxQty": 25}})
    assert rd.order_size() == 25


def test_daily_review_lists_rule_changes():
    from datetime import datetime
    from tradingbot import run_daily_review as review
    _save({"stock_orb": {"trades": 1}})
    text = review.section_rules(datetime.now(rules.ET).date())
    assert "Stock ORB trades per day: 3 per day → 1 per day" in text
    assert "| Stock ORB | Trades per day | 1 per day | 3 per day |" in text
