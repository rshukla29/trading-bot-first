"""Start / Pause / Stop (control.py), the control server's API and its
security checks, and how the bots obey a mode."""
import http.client
import json
import threading
from types import SimpleNamespace

import pytest

from tradingbot import control
from tradingbot.config import settings


# ---------------------------------------------------------------- control file

def test_no_file_means_every_strategy_runs():
    assert control.mode("orb") == control.RUN
    assert control.entries_allowed("orb") and not control.wants_close("orb")


def test_modes_persist_and_close_only_goes_with_stop():
    control.set_mode("orb", control.PAUSE, close=True)          # close ignored on pause
    assert control.mode("orb") == control.PAUSE and not control.wants_close("orb")
    control.set_mode("orb", control.STOP, close=True)
    assert control.mode("orb") == control.STOP and control.wants_close("orb")
    assert control.mode("iron_condor") == control.RUN            # others untouched
    control.close_done("orb")
    assert control.mode("orb") == control.STOP and not control.wants_close("orb")
    log = control.read_all()["log"]
    assert log[0]["text"] == "ORB: positions closed at market" and len(log) == 3


def test_unreadable_file_pauses_everything(tmp_path):
    p = tmp_path / "control.json"
    p.write_text("{not json", encoding="utf-8")
    assert control.mode("orb") == control.PAUSE                 # never lets a paused bot trade
    assert not control.entries_allowed("rsi2")
    control.set_mode("orb", control.RUN)                         # a button press starts a clean file
    assert control.mode("orb") == control.RUN


def test_bad_values_are_refused():
    with pytest.raises(ValueError):
        control.set_mode("nope", control.RUN)
    with pytest.raises(ValueError):
        control.set_mode("orb", "sleep")


def test_gate_logs_each_change_once(caplog):
    gate = control.Gate("orb")
    for _ in range(3):
        gate.entries_allowed()
    control.set_mode("orb", control.PAUSE)
    for _ in range(3):
        assert not gate.entries_allowed()
    assert sum("PAUSED" in r.message for r in caplog.records) == 1


def test_available_hides_bots_missing_from_this_repo(monkeypatch):
    monkeypatch.setitem(control.STRATEGIES, "ghost", dict(name="Ghost", sub="", task="X", module="tradingbot.no_such_bot",
                                                          intraday=True))
    assert "ghost" not in control.available() and "orb" in control.available()


# ---------------------------------------------------------------- server

@pytest.fixture
def server(monkeypatch, tmp_path):
    from tradingbot import run_control_server as srv
    started = []
    monkeypatch.setattr(srv, "task_info", lambda names, fresh=False: {n: {"state": "Ready", "next": "Mon 9:30am"} for n in names})
    monkeypatch.setattr(srv, "start_task", lambda name: started.append(name) or True)
    monkeypatch.setattr(srv, "market_open", lambda now=None: True)
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "none.db"))
    s = srv.make_server(port=0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    port = s.server_address[1]

    def req(method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{port}"}
        if body is not None:
            h.update({"Content-Type": "application/json", "X-Bot-Control": "1", "Origin": f"http://127.0.0.1:{port}"})
        h.update(headers or {})
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        return r.status, (json.loads(data) if r.getheader("Content-Type", "").startswith("application/json") else data)

    yield req, started, port
    s.shutdown()
    s.server_close()


def test_state_lists_strategies_with_modes(server):
    req, _, _ = server
    status, st = req("GET", "/api/state")
    assert status == 200
    ids = [s["id"] for s in st["strategies"]]
    assert "orb" in ids and "iron_condor" in ids
    assert all(s["mode"] == "run" for s in st["strategies"])
    assert [b["name"] for b in st["safety"]] == ["Expiry guard", "Step stop"]


def test_pause_then_start_restarts_an_intraday_bot(server):
    req, started, _ = server
    status, st = req("POST", "/api/control", {"strategy": "orb", "mode": "pause"})
    assert status == 200 and control.mode("orb") == "pause" and started == []
    status, st = req("POST", "/api/control", {"strategy": "orb", "mode": "run"})
    assert status == 200 and control.mode("orb") == "run"
    assert started == ["ORBTradingBot"]                          # market open, task not running


def test_stop_and_close_starts_the_bot_that_must_sell(server):
    req, started, _ = server
    req("POST", "/api/control", {"strategy": "iron_condor", "mode": "stop", "close": True})
    req("POST", "/api/control", {"strategy": "rsi2", "mode": "stop", "close": True})
    assert control.wants_close("iron_condor") and control.wants_close("rsi2")
    assert started == ["IronCondorTradingBot", "StepStopTradingBot"]   # stocks: the client-1 step-stop bot


def test_pause_all(server):
    req, _, _ = server
    status, st = req("POST", "/api/control", {"strategy": "all", "mode": "pause"})
    assert status == 200 and all(s["mode"] == "pause" for s in st["strategies"])
    assert "paused" in st["message"]


@pytest.mark.parametrize("change", [
    {"headers": {"Origin": "https://evil.example"}},             # another site's page
    {"headers": {"X-Bot-Control": ""}},                          # a plain form post can't set it
    {"headers": {"Content-Type": "text/plain"}},
    {"host": "evil.example:8765"},                               # DNS rebinding
])
def test_requests_not_from_the_dashboard_are_refused(server, change):
    req, _, _ = server
    status, body = req("POST", "/api/control", {"strategy": "orb", "mode": "stop"}, **change)
    assert status == 403 and control.mode("orb") == "run"


def test_bad_requests(server):
    req, _, _ = server
    assert req("POST", "/api/control", {"strategy": "orb", "mode": "sleep"})[0] == 400
    assert req("POST", "/api/control", {"strategy": "nope", "mode": "run"})[0] == 400


def test_only_reports_and_analysis_are_served(server, tmp_path, monkeypatch):
    req, _, _ = server
    assert req("GET", "/.env")[0] == 404
    assert req("GET", "/reports/../.env")[0] == 404
    assert req("GET", "/data/journal.db")[0] == 404


def test_controls_page_without_a_dashboard(server, monkeypatch, tmp_path):
    req, _, port = server
    monkeypatch.setattr(settings, "reports_dir", str(tmp_path / "nothing"))
    status, body = req("GET", "/")
    assert status == 302
    status, body = req("GET", "/controls")
    assert status == 200 and b'id="cp-root"' in body and b"{{CONTROL_PORT}}" not in body


# ---------------------------------------------------------------- bots

def test_orb_takes_no_entry_while_paused_but_still_manages(monkeypatch):
    import tradingbot.run_orb_bot as orb
    monkeypatch.setattr(orb, "check_entry", lambda *a, **k: pytest.fail("entered while paused"))
    monkeypatch.setattr(orb, "fetch_intraday_bars", lambda *a, **k: pytest.fail("fetched bars while paused"))
    control.set_mode("orb", control.PAUSE)
    state = {"symbol": "SPY", "underlying": None, "chain": None, "position": None, "last_bar_ts": None,
             "entries_today": {"C": 0, "P": 0}}
    orb.tick_symbol(None, None, None, None, state)               # nothing happens

    seen = []
    monkeypatch.setattr(orb, "check_exit", lambda *a, force_reason=None: seen.append(force_reason) or False)
    state["position"] = {"contract": None}
    orb.tick_symbol(None, None, None, None, state)
    assert seen == [None]                                        # still checked by its own rules


def test_orb_stop_and_close_forces_the_exit_now(monkeypatch):
    import tradingbot.run_orb_bot as orb
    seen = []
    monkeypatch.setattr(orb, "check_exit", lambda *a, force_reason=None: seen.append(force_reason) or True)
    control.set_mode("orb", control.STOP, close=True)
    state = {"symbol": "SPY", "underlying": None, "position": {"contract": None, "last_check": 1e18}}
    orb.tick_symbol(SimpleNamespace(), None, None, None, state)  # no 60s wait for a forced close
    assert seen == ["manual_stop"] and state["position"] is None


def test_condor_force_close_ignores_the_rules(tmp_path, monkeypatch):
    import tradingbot.run_iron_condor_bot as bot
    from tests.test_condor_partial_close import _journal, _setup
    j, _ = _journal(tmp_path)
    ib, sent, _ = _setup(monkeypatch, held=10, fills=[10])
    monkeypatch.setattr(bot, "current_cost", lambda ib, legs: 1.00)  # no rule would close at this cost
    bot.manage_position(ib, j, "SPY", j.open_condor_positions("SPY")[0], force_reason="manual_stop")
    assert sent == [10] and j.open_condor_positions("SPY") == []


def test_close_owned_sells_only_the_strategys_own_stocks(tmp_path, monkeypatch):
    from tradingbot import stock_close
    from tradingbot.journal import Journal
    j = Journal(str(tmp_path / "j.db"))
    j.claim_stock("KO", "rsi2")
    j.claim_stock("XOM", "ma_crossover")
    sold = []
    monkeypatch.setattr(stock_close, "place_market_sell", lambda ib, s, q: sold.append((s, q)) or object())
    out = stock_close.close_owned(None, j, "rsi2", {"KO": 40, "XOM": 10, "ASTX": 100})
    assert out == ["KO"] and sold == [("KO", 40)]                 # never XOM (other bot) or ASTX (manual)
