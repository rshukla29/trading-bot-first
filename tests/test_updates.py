"""Versions, the daily update check, the Update now button and the release
changelog (updates.py, run_control_server.apply_update, scripts/release.py)."""
import json
import subprocess
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from tradingbot import updates
from tradingbot.updates import ET

CHANGELOG_110 = """# Changelog

## Unreleased

## 1.1.0 — 2026-12-01

- Condor exits are faster.
- A long note that wraps
  onto a second line.

## 1.0.1 — 2026-11-20

- Fix: dashboard date.

## 1.0.0 — 2026-11-16

- First release.
"""


def test_versions_compare_as_numbers():
    assert updates.parse("1.10.0") > updates.parse("v1.9.3")
    assert updates.parse("1.0") is None and updates.parse("v1.2.3-beta") is None
    assert updates.latest_tag(["v1.9.0", "v1.10.0", "junk", "v1.2.0"]) == "v1.10.0"
    assert updates.latest_tag([]) is None


def test_this_copy_has_a_version():
    assert updates.parse(updates.current())


def test_notes_cover_every_version_since_yours():
    notes = updates.notes_between(CHANGELOG_110, "1.0.0", "v1.1.0")
    assert [n["version"] for n in notes] == ["1.1.0", "1.0.1"]
    assert notes[0]["items"] == ["Condor exits are faster.", "A long note that wraps onto a second line."]
    assert notes[0]["date"] == "2026-12-01"


def test_cut_release_moves_unreleased_notes_under_the_version():
    text = "# Changelog\n\n## Unreleased\n\n- New thing.\n- Fix thing.\n\n## 1.0.0 — 2026-11-16\n\n- First.\n"
    new, items = updates.cut_release(text, "1.1.0", "2026-12-01")
    assert items == ["New thing.", "Fix thing."]
    assert "## Unreleased\n\n## 1.1.0 — 2026-12-01\n\n- New thing.\n- Fix thing.\n\n## 1.0.0" in new
    assert updates.notes_between(new, "1.0.0", "1.1.0")[0]["items"] == items


def test_cut_release_refuses_without_notes():
    with pytest.raises(ValueError, match="Nothing under"):
        updates.cut_release("## Unreleased\n\n## 1.0.0 — x\n- a\n", "1.1.0", "d")
    with pytest.raises(ValueError, match="no '## Unreleased'"):
        updates.cut_release("# Changelog\n", "1.1.0", "d")


def test_the_real_changelog_is_releasable_or_released():
    from pathlib import Path
    text = (Path(updates.REPO) / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## Unreleased" in text


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def cloned(tmp_path, monkeypatch):
    """A repo whose working tree is 1.0.0 with a v1.1.0 tag ahead of it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.0.0"\n', encoding="utf-8")
    (repo / "CHANGELOG.md").write_text("## Unreleased\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "1.0.0")
    _git(repo, "tag", "v1.0.0")
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.1.0"\n', encoding="utf-8")
    (repo / "CHANGELOG.md").write_text(CHANGELOG_110, encoding="utf-8")
    _git(repo, "commit", "-qam", "1.1.0")
    _git(repo, "tag", "v1.1.0")
    _git(repo, "checkout", "-q", "v1.0.0")
    monkeypatch.setattr(updates, "REPO", repo)
    return repo


def test_check_finds_a_newer_release_with_its_notes(cloned):
    s = updates.check(fetch=False)
    assert s["available"] and (s["current"], s["latest"]) == ("1.0.0", "1.1.0") and s["error"] is None
    assert [n["version"] for n in s["notes"]] == ["1.1.0", "1.0.1"]
    p = updates.payload()
    assert p["available"] and p["latest"] == "1.1.0" and p["notes"]


def test_no_banner_once_installed(cloned):
    updates.check(fetch=False)
    _git(cloned, "checkout", "-q", "v1.1.0")  # updated: the saved check is now stale
    assert not updates.payload()["available"]
    assert not updates.check(fetch=False)["available"]


def test_check_survives_being_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "REPO", tmp_path)  # not a git repo
    s = updates.check(fetch=True)
    assert not s["available"] and s["error"]


def test_bots_busy_on_weekdays_8am_to_515pm():
    assert updates.bots_busy(datetime(2026, 11, 16, 9, 0, tzinfo=ET))
    assert not updates.bots_busy(datetime(2026, 11, 16, 17, 30, tzinfo=ET))
    assert not updates.bots_busy(datetime(2026, 11, 15, 12, 0, tzinfo=ET))  # Sunday


def test_one_notification_per_version(monkeypatch):
    from tradingbot import notify
    popped = []
    monkeypatch.setattr(updates.sys, "platform", "win32")
    monkeypatch.setattr(notify, "windows_notify", lambda title, text: popped.append(text) or True)
    status = {"available": True, "current": "1.0.0", "latest": "1.1.0", "notified": None}
    assert updates.notify_once(status)
    assert not updates.notify_once(updates.read_status())       # same version: no second one
    assert "1.1.0" in popped[0] and len(popped) == 1


def test_a_stuck_update_stops_showing_as_running():
    updates.mark_running("1.0.0", "1.1.0")
    assert updates.last_result()["running"]
    later = datetime.now(ET) + timedelta(minutes=20)
    r = updates.last_result(now=later)
    assert r["ok"] is False and "didn't report back" in r["message"]


def test_result_written_by_powershell_with_a_bom_is_read():
    updates.RESULT.write_bytes(b'\xef\xbb\xbf{"ok": true, "message": "Updated from 1.0.0 to 1.1.0."}')
    assert updates.last_result()["ok"] is True


# ---------------------------------------------------------------- Update now

@pytest.fixture
def offered(monkeypatch):
    from tradingbot import run_control_server as srv
    started = []
    monkeypatch.setattr(updates, "payload", lambda: {"available": True, "current": "1.0.0", "latest": "1.1.0",
                                                     "result": None, "busy": False, "notes": []})
    monkeypatch.setattr(updates, "bots_busy", lambda now=None: False)
    monkeypatch.setattr(updates, "start_update", lambda v: started.append(v))
    monkeypatch.setattr(srv, "state_payload", lambda: {"ok": 1})
    monkeypatch.setattr(srv, "task_info", lambda fresh=False: {"OrbBot": {"state": "Ready"},
                                                              "BotControlServer": {"state": "Running"}})
    return srv, started


def test_update_now_starts_the_update(offered):
    srv, started = offered
    _, msg = srv.apply_update({"version": "1.1.0"})
    assert started == ["1.1.0"] and "Updating to 1.1.0" in msg
    assert updates.last_result()["running"]


def test_update_now_waits_for_the_bots(offered, monkeypatch):
    srv, started = offered
    monkeypatch.setattr(updates, "bots_busy", lambda now=None: True)
    with pytest.raises(ValueError, match="after 5:15pm"):
        srv.apply_update({"version": "1.1.0"})
    monkeypatch.setattr(updates, "bots_busy", lambda now=None: False)
    monkeypatch.setattr(srv, "task_info", lambda fresh=False: {"RSI2Bot": {"state": "Running"}})
    with pytest.raises(ValueError, match="RSI2Bot"):
        srv.apply_update({"version": "1.1.0"})
    assert started == []


def test_update_now_only_installs_the_offered_version(offered):
    srv, started = offered
    with pytest.raises(ValueError, match="isn't the one on offer"):
        srv.apply_update({"version": "9.9.9"})
    assert started == []


def test_update_endpoint_has_the_same_security_checks(monkeypatch, tmp_path):
    """A web page on another site can't press Update now either."""
    import http.client
    import threading
    from tradingbot import run_control_server as srv
    called = []
    monkeypatch.setattr(srv, "apply_update", lambda body: called.append(body) or ({}, "ok"))
    s = srv.make_server(port=0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    port = s.server_address[1]
    try:
        def post(origin):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("POST", "/api/update", body=json.dumps({"version": "1.1.0"}),
                      headers={"Host": f"127.0.0.1:{port}", "Origin": origin, "Content-Type": "application/json",
                               "X-Bot-Control": "1"})
            return c.getresponse().status
        assert post("https://evil.example") == 403 and called == []
        assert post(f"http://127.0.0.1:{port}") == 200 and called == [{"version": "1.1.0"}]
    finally:
        s.shutdown()
        s.server_close()


def test_release_script_refuses_a_bad_version_number(monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("release", Path(updates.REPO) / "scripts" / "release.py")
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    monkeypatch.setattr(release.sys, "argv", ["release.py", "1.1"])
    with pytest.raises(SystemExit, match="Usage"):
        release.main()


def test_an_old_failure_stops_showing_once_the_copy_is_newer(monkeypatch):
    updates.RESULT.write_text('{"ok": false, "from": "1.3.0", "to": "1.3.0", "message": "Update to 1.4.0 failed"}')
    monkeypatch.setattr(updates, "current", lambda: "1.3.0")
    assert updates.last_result()["ok"] is False          # still on 1.3.0: show the failure
    monkeypatch.setattr(updates, "current", lambda: "1.4.0")
    assert updates.last_result() is None                 # updated another way since: hide it
