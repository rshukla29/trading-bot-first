"""Readiness check (readiness.py): each check's verdict, the summary, a
crash part-way, and the Run checks now endpoint. No real TWS is needed."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from tradingbot import readiness as rd
from tradingbot.config import settings
from tradingbot.readiness import ET, FAIL, PASS, SKIP, WARN


def test_live_trading_or_a_live_port_fails(monkeypatch):
    monkeypatch.setattr(settings, "live_trading", True)
    assert rd.check_settings()["status"] == FAIL
    monkeypatch.setattr(settings, "live_trading", False)
    monkeypatch.setattr(settings, "ibkr_port", 7496)
    assert rd.check_settings()["status"] == FAIL
    monkeypatch.setattr(settings, "ibkr_port", 7497)
    assert rd.check_settings()["status"] == PASS


def test_trade_diary_folder_must_exist(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "nope" / "j.db"))
    assert rd.check_journal()["status"] == FAIL
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "j.db"))
    c = rd.check_journal()
    assert c["status"] == PASS and "created at the first trade" in c["detail"]


def test_sleep_setting():
    assert rd.check_sleep(0)["status"] == PASS
    c = rd.check_sleep(30)
    assert c["status"] == WARN and "30 minutes" in c["detail"] and "Never" in c["fix"]


def test_tasks_missing_disabled_or_off_schedule(monkeypatch):
    from tradingbot import run_control_server as srv
    monkeypatch.setattr(srv, "schedule", lambda: [{"name": "OrbBot", "ny": "09:30", "script": "run_orb_scheduled.ps1"},
                                                  {"name": "RSI2TradingBot", "ny": "16:30", "script": "run_rsi2_scheduled.ps1"}])
    monkeypatch.setattr(srv, "schedule_warnings", lambda tasks, plan=None: [])
    monkeypatch.setattr(srv, "task_info", lambda fresh=False: {"OrbBot": {"state": "Ready"}})
    c = rd.check_tasks()
    assert c["status"] == FAIL and "RSI2TradingBot" in c["detail"] and "install.ps1" in c["fix"]
    monkeypatch.setattr(srv, "task_info", lambda fresh=False: {"OrbBot": {"state": "Ready"}, "RSI2TradingBot": {"state": "Disabled"}})
    assert "Disabled" in rd.check_tasks()["detail"]
    monkeypatch.setattr(srv, "task_info", lambda fresh=False: {"OrbBot": {"state": "Ready"}, "RSI2TradingBot": {"state": "Ready"}})
    monkeypatch.setattr(srv, "schedule_warnings", lambda tasks, plan=None: ["OrbBot runs at 8:30am, not 9:30am"])
    c = rd.check_tasks()
    assert c["status"] == FAIL and "register_tasks.ps1" in c["fix"]
    monkeypatch.setattr(srv, "schedule_warnings", lambda tasks, plan=None: [])
    assert rd.check_tasks()["status"] == PASS


def test_expected_account_comes_from_the_paths(monkeypatch):
    monkeypatch.setattr(settings, "db_path", "data/journal_DUR356762.db")
    assert rd.expected_account() == "DUR356762"
    monkeypatch.setattr(settings, "db_path", "data/journal.db")
    monkeypatch.setattr(settings, "reports_dir", "reports")
    assert rd.expected_account() is None


@pytest.mark.parametrize("codes,got,delayed,in_hours,want", [
    ({354: "Requested market data is not subscribed"}, False, False, False, FAIL),     # options sub missing
    ({10089: "requires additional subscription"}, False, False, True, FAIL),
    ({2186: "..."}, True, False, False, FAIL),                                          # new account, not shared
    ({10197: "competing live session"}, False, False, True, FAIL),
    ({}, True, True, True, FAIL),                                                        # delayed only
    ({}, True, False, True, PASS),
    ({}, False, False, True, WARN),                                                      # market open, nothing came
    ({}, False, False, False, PASS),                                                     # weekend, no errors
])
def test_market_data_verdicts(codes, got, delayed, in_hours, want):
    assert rd.data_status("x", codes, got, delayed, in_hours)[0] == want


def test_market_hours():
    assert rd.market_hours(datetime(2026, 11, 16, 10, 0, tzinfo=ET))
    assert not rd.market_hours(datetime(2026, 11, 16, 8, 0, tzinfo=ET))
    assert not rd.market_hours(datetime(2026, 11, 15, 12, 0, tzinfo=ET))


def test_summary_counts_skips_as_not_ready():
    s = rd.summary([rd.check("a", "pc", "A", PASS), rd.check("b", "pc", "B", WARN)])
    assert s["ready"] and s["warn"] == 1
    assert not rd.summary([rd.check("a", "pc", "A", PASS), rd.check("b", "ibkr", "B", SKIP)])["ready"]


def test_fix_text_only_on_problems():
    assert rd.check("a", "pc", "A", PASS, "fine", "do this")["fix"] == ""
    assert rd.check("a", "pc", "A", FAIL, "bad", "do this")["fix"] == "do this"


def test_tws_closed_fails_and_skips_the_rest(monkeypatch):
    import ib_async
    monkeypatch.setattr(ib_async, "IB", lambda: _NoTWS())
    out = rd.ibkr_checks([])
    assert out[0]["id"] == "tws" and out[0]["status"] == FAIL and "TWS" in out[0]["fix"]
    assert {c["status"] for c in out[1:]} == {SKIP} and len(out) == 1 + len(rd.IBKR_LATER)


class _Event(list):
    def __iadd__(self, f):
        self.append(f)
        return self


class _NoTWS:
    def __init__(self):
        self.errorEvent = _Event()

    def connect(self, *a, **k):
        raise ConnectionRefusedError()


def test_a_crash_part_way_keeps_results_and_is_not_ready(monkeypatch):
    def half(out):
        out.append(rd.check("tws", "ibkr", "TWS connection", PASS, "ok"))
        raise RuntimeError("boom")
    monkeypatch.setattr(rd, "pc_checks", lambda: [rd.check("settings", "pc", "Safe settings", PASS)])
    monkeypatch.setattr(rd, "ibkr_checks", lambda out, wait_minutes=0: half(out))
    data = rd.run()
    ids = [c["id"] for c in data["checks"]]
    assert ids[:2] == ["settings", "tws"] and "orders" in ids and ids.count("tws") == 1
    assert not rd.summary(data["checks"])["ready"] and not data["running"]
    assert json.loads(rd.RESULTS.read_text(encoding="utf-8"))["checks"] == data["checks"]


def test_notifies_at_8am_only_when_something_fails(monkeypatch):
    from tradingbot import notify
    shown = []
    monkeypatch.setattr(notify, "windows_notify", lambda title, text: shown.append(text) or True)
    monkeypatch.setattr(rd, "ibkr_checks", lambda out, wait_minutes=0: out)
    monkeypatch.setattr(rd, "pc_checks", lambda: [rd.check("settings", "pc", "Safe settings", PASS)])
    rd.run(notify=True)
    assert shown == []
    monkeypatch.setattr(rd, "pc_checks", lambda: [rd.check("tasks", "pc", "Scheduled tasks", FAIL, "Missing: OrbBot")])
    rd.run(notify=True)
    assert len(shown) == 1 and "Scheduled tasks" in shown[0]


def test_8am_run_waits_for_tws_and_stays_marked_running(monkeypatch):
    launched = []
    monkeypatch.setattr(rd.subprocess, "Popen", lambda args, **k: launched.append(args))
    assert rd.start(notify=True, wait_minutes=10)
    assert launched[0][-3:] == ["--notify", "--wait", "10"]
    d = rd.read()
    later = datetime.fromisoformat(d["started_at"]) + timedelta(minutes=8)   # still waiting for TWS
    assert rd.running(d, now=later) and rd.payload()["waiting_for_tws"]


def test_running_mark_expires():
    started = datetime.now(ET)
    assert rd.running({"running": True, "started_at": started.isoformat()}, now=started + timedelta(seconds=30))
    assert not rd.running({"running": True, "started_at": started.isoformat()}, now=started + timedelta(minutes=5))


def test_start_runs_in_the_background_once(monkeypatch):
    launched = []
    monkeypatch.setattr(rd.subprocess, "Popen", lambda args, **k: launched.append(args))
    assert rd.start() is True
    assert launched[0][1:3] == ["-m", "tradingbot.run_readiness"] and rd.payload()["running"]
    assert rd.start() is False and len(launched) == 1   # already running


def test_state_and_run_checks_endpoint(monkeypatch, tmp_path):
    import http.client
    import threading
    from tradingbot import run_control_server as srv
    monkeypatch.setattr(srv, "task_info", lambda fresh=False: {})
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "none.db"))
    launched = []
    monkeypatch.setattr(rd.subprocess, "Popen", lambda args, **k: launched.append(args))
    s = srv.make_server(port=0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    port = s.server_address[1]
    try:
        def post(origin):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("POST", "/api/readiness", body="{}", headers={"Host": f"127.0.0.1:{port}", "Origin": origin,
                                                                   "Content-Type": "application/json", "X-Bot-Control": "1"})
            r = c.getresponse()
            return r.status, json.loads(r.read())
        assert post("https://evil.example")[0] == 403 and launched == []
        status, body = post(f"http://127.0.0.1:{port}")
        assert status == 200 and len(launched) == 1 and body["readiness"]["running"]
        assert "Running the checks" in body["message"]
    finally:
        s.shutdown()
        s.server_close()
