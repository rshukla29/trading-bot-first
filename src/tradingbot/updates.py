"""Versions and update notices.

The version lives in pyproject.toml; each release is a git tag vX.Y.Z on
trading-bot-first with its notes in CHANGELOG.md (scripts/release.py).

An installed copy checks once a day, when the control server starts: it
runs `git fetch --tags` against the repo it was cloned from (no API key,
nothing sent but the fetch) and compares the newest vX.Y.Z tag with its own
version. A newer one shows a banner on the Bot controls tab with the
release notes and an Update now button (scripts/update.ps1, only while the
bots are idle), plus one Windows notification per new version.

    data/update_status.json   last check: current, latest, notes, notified
    data/update_result.json   written by update.ps1: the last update's outcome
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tomllib
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]
STATUS = REPO / "data" / "update_status.json"
RESULT = REPO / "data" / "update_result.json"
ET = ZoneInfo("America/New_York")
TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
# The bots run Mon-Fri from the 8am snapshot to the 4:50pm review: no updates then.
BUSY_START, BUSY_END = time(8, 0), time(17, 15)


def parse(version: str) -> tuple[int, int, int] | None:
    m = TAG.match(version if version.startswith("v") else "v" + version)
    return tuple(int(x) for x in m.groups()) if m else None


def current() -> str:
    """This copy's version, from pyproject.toml."""
    try:
        return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    except (OSError, KeyError, ValueError):
        return "0.0.0"


def _git(*args: str, timeout: float = 30.0) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never hang on a password prompt
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    out = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=timeout,
                         env=env, creationflags=flags, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300] or f"git {args[0]} failed")
    return out.stdout


def latest_tag(tags: list[str]) -> str | None:
    good = [t for t in tags if parse(t)]
    return max(good, key=parse) if good else None


def notes_between(changelog: str, have: str, latest: str) -> list[dict]:
    """CHANGELOG.md sections newer than `have`, up to and including `latest`:
    [{"version", "date", "items": [...]}], newest first."""
    lo, hi = parse(have) or (0, 0, 0), parse(latest)
    out, sec = [], None
    for line in changelog.splitlines():
        m = re.match(r"^##\s+\[?v?(\d+\.\d+\.\d+)\]?\s*(?:[-–—]\s*(\S+))?", line)
        if m:
            v = parse(m.group(1))
            sec = {"version": m.group(1), "date": m.group(2) or "", "items": []} if hi and lo < v <= hi else None
            if sec:
                out.append(sec)
        elif line.startswith("## "):
            sec = None  # e.g. "## Unreleased"
        elif sec is not None and re.match(r"^\s*[-*]\s+", line):
            sec["items"].append(re.sub(r"^\s*[-*]\s+", "", line).strip())
        elif sec is not None and line.startswith("  ") and line.strip() and sec["items"]:
            sec["items"][-1] += " " + line.strip()  # a wrapped bullet
    return out


def cut_release(changelog: str, version: str, day: str) -> tuple[str, list[str]]:
    """Turn CHANGELOG.md's "## Unreleased" notes into "## X.Y.Z — day" (a
    fresh empty Unreleased stays on top). Returns the new text and the notes;
    ValueError if there are no notes to release."""
    lines = changelog.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if re.match(r"^##\s+Unreleased\s*$", l, re.I))
    except StopIteration:
        raise ValueError("CHANGELOG.md has no '## Unreleased' section") from None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    body = lines[start + 1:end]
    items = [re.sub(r"^\s*[-*]\s+", "", l).strip() for l in body if re.match(r"^\s*[-*]\s+", l)]
    if not items:
        raise ValueError("Nothing under '## Unreleased' in CHANGELOG.md: add a line per change first")
    while body and not body[-1].strip():
        body.pop()
    new = lines[:start] + ["## Unreleased", "", f"## {version} — {day}"] + body + [""] + lines[end:]
    return "\n".join(new).rstrip() + "\n", items


def read_status() -> dict:
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def check(fetch: bool = True) -> dict:
    """Look for a newer release; saves and returns the status."""
    old = read_status()
    have = current()
    status = {"checked_at": datetime.now(ET).isoformat(timespec="seconds"), "current": have,
              "latest": have, "available": False, "notes": [], "error": None,
              "notified": old.get("notified")}
    try:
        if fetch:
            _git("fetch", "--tags", "--quiet", "origin")
        latest = latest_tag(_git("tag", "--list", "v*").split())
        if latest and parse(latest) > (parse(have) or (0, 0, 0)):
            status.update(latest=latest.lstrip("v"), available=True)
            try:
                status["notes"] = notes_between(_git("show", f"{latest}:CHANGELOG.md"), have, latest)
            except RuntimeError:
                pass
    except Exception as exc:  # offline, no git, no tags: try again tomorrow
        status["error"] = str(exc)[:300]
        logger.info("Update check failed: %s", exc)
    _write(STATUS, status)
    return status


def bots_busy(now: datetime | None = None) -> bool:
    """Mon-Fri 8am-5:15pm New York: the bots and reports may be running."""
    now = now or datetime.now(ET)
    return now.weekday() < 5 and BUSY_START <= now.time() < BUSY_END


def last_result(now: datetime | None = None) -> dict | None:
    try:
        r = json.loads(RESULT.read_text(encoding="utf-8-sig"))  # PowerShell writes a BOM
    except (OSError, ValueError):
        return None
    if r.get("running"):
        try:
            age = ((now or datetime.now(ET)) - datetime.fromisoformat(r["at"])).total_seconds()
        except (KeyError, ValueError, TypeError):
            age = 1e9
        if age > 15 * 60:  # update.ps1 never reported back
            return {"ok": False, "from": r.get("from"), "to": r.get("to"), "at": r.get("at"),
                    "message": "The last update didn't report back. See the logs folder (update_*.log)."}
    return r


def payload() -> dict:
    """What the Bot controls tab shows."""
    s = read_status()
    have = current()
    available = bool(s.get("available")) and (parse(s.get("latest", "")) or (0,)) > (parse(have) or (0,))
    return {"current": have, "latest": s.get("latest", have) if available else have, "available": available,
            "notes": s.get("notes", []) if available else [], "checked_at": s.get("checked_at"),
            "busy": bots_busy(), "result": last_result()}


def notify_once(status: dict) -> bool:
    """One Windows notification per new version. True if one was shown."""
    if not status.get("available") or status.get("notified") == status.get("latest") or sys.platform != "win32":
        return False
    from tradingbot import notify
    text = f"Version {status['latest']} is available (you have {status['current']}). Open the dashboard's Bot controls tab to update."
    if not notify.windows_notify("Trading bots update", text):
        return False
    _write(STATUS, {**status, "notified": status["latest"]})
    return True


def mark_running(have: str, to: str) -> None:
    """Shown until update.ps1 writes the outcome (a stale mark, e.g. after a
    crash, is ignored after 15 minutes)."""
    _write(RESULT, {"running": True, "from": have, "to": to, "at": datetime.now(ET).isoformat(timespec="seconds")})


def start_update(version: str) -> None:
    """Run scripts/update.ps1 in the background. It stops and restarts the
    control server itself, so it must outlive this process."""
    log_dir = REPO / "logs"
    log_dir.mkdir(exist_ok=True)
    log = open(log_dir / f"update_{datetime.now(ET):%Y-%m-%d}.log", "ab")
    subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                      "-File", str(REPO / "scripts" / "update.ps1"), "-Version", "v" + version.lstrip("v")],
                     cwd=str(REPO), stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
