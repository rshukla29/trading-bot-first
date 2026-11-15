"""Local control server behind the dashboard's Start / Pause / Stop buttons.

Listens on 127.0.0.1:CONTROL_PORT (8765) only, so nothing outside this PC
can reach it, and serves:
  /             the dashboard (reports dir / dashboard.html), with a live
                Bot controls tab; /controls until the first dashboard exists
  /controls     the controls on their own
  /api/state    each strategy's mode, open positions, today's P&L, task state
  /api/control  POST {"strategy": id | "all", "mode": run|pause|stop, "close": bool}
  /reports/..., /analysis/...   the report and review pages the dashboard links to

A button press writes data/control.json (control.py); the bots read it every
tick. When the market is open it also starts the bot that has to act: Start
on an all-day bot that isn't running, or Stop + close on a bot that isn't
running (RSI(2) / MA crossover stocks are sold by the step-stop bot, which
holds client 1). It never places orders itself.

Every POST must come from the dashboard page itself: same Host and Origin
(127.0.0.1 or localhost on this port), a JSON body and the X-Bot-Control
header. A web page on another site can't send that, so it can't press the
buttons through your browser.

    python -m tradingbot.run_control_server
    python -m tradingbot.run_control_server --open --until 18:00
    python -m tradingbot.run_control_server --open --now --idle 30

--open rebuilds the dashboard and opens it
in the browser, or, if a dashboard page is still open from before, lets that
page reload itself instead of opening another; --until stops the server at
that time (New York), leaving the browser alone. The BotControlServer
task runs it that way Monday-Friday at 8am and at logon, only between 8am
and 6pm (scripts/run_control_server.ps1).

The desktop shortcut (run_dashboard --open) starts it the second way when
it isn't running, at any hour: --now opens the page straight away, and
--idle stops it after that many minutes with no dashboard page open,
except during Mon-Fri 8am-6pm, when it stands in for the scheduled one.
/api/ping answers "up?" without counting as a page checking in.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import sqlite3
import subprocess
import sys
import threading
import time as clock
from datetime import datetime, time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from tradingbot import control
from tradingbot.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
MARKET_OPEN, MARKET_CLOSE = time(9, 30), time(16, 0)
PANEL = Path(__file__).with_name("control_panel.html")
SAFETY = (("Expiry guard", "ExpiryGuardBot"), ("Step stop", "StepStopTradingBot"))
STOCK_BOTS = ("rsi2", "ma_crossover")  # closed by the step-stop bot (client 1) during market hours
DISPLAY_TO_ID = {"ORB (SPY 0DTE options)": "orb", "ORB credit spreads": "credit_orb", "Iron condor": "iron_condor",
                 "Trend Join": "trend_join", "Stock ORB": "stock_orb", "RSI(2)": "rsi2", "MA-crossover": "ma_crossover"}
STATIC_ROOTS = ("reports", "analysis")
MAX_BODY = 4096
TASK_CACHE_SECONDS = 60.0  # each refresh starts PowerShell (~0.5s CPU); next-run times don't change minute to minute
SCHEDULE = Path(__file__).resolve().parents[2] / "scripts" / "schedule.json"


def market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(ET)
    return now.weekday() < 5 and MARKET_OPEN <= now.time() < MARKET_CLOSE


def _when(ts: str | datetime | None) -> str:
    """'Mon 10:42am' for an ISO timestamp or datetime."""
    if not ts:
        return ""
    t = ts if isinstance(ts, datetime) else datetime.fromisoformat(ts)
    return t.strftime("%a %I:%M%p").replace(" 0", " ").replace("AM", "am").replace("PM", "pm")


# ---------------------------------------------------------------- tasks

_task_cache: dict = {"at": 0.0, "data": {}}
_task_lock = threading.Lock()


def _powershell(cmd: str, timeout: float = 20.0) -> str:
    out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
                         capture_output=True, text=True, timeout=timeout)
    return out.stdout.strip()


def schedule() -> list[dict]:
    """scripts/schedule.json's tasks: [{"name", "ny": "HH:MM", "script"}]."""
    try:
        return json.loads(SCHEDULE.read_text(encoding="utf-8"))["tasks"]
    except (OSError, ValueError, KeyError):
        logger.exception("Couldn't read %s", SCHEDULE)
        return []


def all_task_names() -> list[str]:
    return sorted({m["task"] for m in control.STRATEGIES.values()} | {t for _, t in SAFETY}
                  | {t["name"] for t in schedule()})


def task_info(fresh: bool = False) -> dict[str, dict]:
    """Every known task: {name: {"state": Ready|Running|Disabled|Missing,
    "next": "Mon 9:30am", "next_at": ISO local time or ""}}, cached for a few
    seconds (each PowerShell call takes ~1s)."""
    names = all_task_names()
    with _task_lock:
        if not fresh and clock.monotonic() - _task_cache["at"] < TASK_CACHE_SECONDS:
            return _task_cache["data"]
        cmd = ("Get-ScheduledTask -TaskName " + ",".join(f"'{n}'" for n in names) + " -ErrorAction SilentlyContinue | "
               "ForEach-Object { $i = $_ | Get-ScheduledTaskInfo; [pscustomobject]@{n=$_.TaskName; s=[string]$_.State; "
               "r=$(if ($i.NextRunTime) { $i.NextRunTime.ToString('s') } else { '' })} } | ConvertTo-Json -Compress")
        data = {n: {"state": "Missing", "next": "", "next_at": ""} for n in names}
        try:
            raw = _powershell(cmd)
            rows = json.loads(raw) if raw else []
            for r in rows if isinstance(rows, list) else [rows]:
                data[r["n"]] = {"state": r["s"], "next": _when(r["r"]) if r["r"] else "", "next_at": r["r"] or ""}
        except Exception:
            logger.exception("Couldn't read the scheduled tasks")
        _task_cache.update(at=clock.monotonic(), data=data)
        return data


def start_task(name: str) -> bool:
    try:
        _powershell(f"Start-ScheduledTask -TaskName '{name}'")
        logger.info("Started task %s", name)
        _task_cache["at"] = 0.0
        return True
    except Exception:
        logger.exception("Couldn't start task %s", name)
        return False


# ---------------------------------------------------------------- state

def _journal_rows() -> tuple[list[dict], list[dict]]:
    """(open positions, closed trades) from the journal, via the dashboard's own readers."""
    if not Path(settings.db_path).exists():
        return [], []
    from tradingbot.dashboard import closed_trades, open_positions
    conn = sqlite3.connect(settings.db_path)
    try:
        return open_positions(conn), closed_trades(conn)
    finally:
        conn.close()


def state_payload() -> dict:
    strategies = control.available()
    try:
        data = control.read_all()
        error = None
    except control.ControlFileError as exc:
        data, error = {"strategies": {}, "log": []}, f"Control file unreadable, every strategy is paused: {exc}"
    tasks = task_info()
    try:
        open_rows, trades = _journal_rows()
    except Exception:
        logger.exception("Couldn't read the journal")
        open_rows, trades = [], []
    today = datetime.now(ET).date().isoformat()
    out = []
    for sid, meta in strategies.items():
        rec = data["strategies"].get(sid, {})
        mode = rec.get("mode", control.RUN) if not error else control.PAUSE
        mine = [p for p in open_rows if DISPLAY_TO_ID.get(p["strategy"]) == sid]
        todays = [t for t in trades if DISPLAY_TO_ID.get(t["strategy"]) == sid and t["date"] == today]
        task = tasks.get(meta["task"], {"state": "Missing", "next": ""})
        out.append(dict(
            id=sid, name=meta["name"], sub=meta["sub"], mode=mode,
            close=bool(rec.get("close")) and mode == control.STOP,
            since=rec.get("since"), since_text=_when(rec.get("since")), by=rec.get("by"),
            task=meta["task"], task_state=task["state"], next_run=task["next"],
            positions=[{"symbol": p["symbol"], "qty": p["qty"],
                        "label": p["symbol"] + (f" ×{p['qty']:g}" if isinstance(p["qty"], (int, float)) else "")}
                       for p in mine],
            today_pnl=round(sum(t["pnl"] for t in todays), 2) if todays else 0.0, today_trades=len(todays),
        ))
    return dict(
        strategies=out, error=error, market_open=market_open(), updated=_when(datetime.now(ET)),
        safety=[{"name": n, "task": t, "state": tasks.get(t, {}).get("state", "Missing")} for n, t in SAFETY],
        log=[{**entry, "at_text": _when(entry.get("at"))} for entry in data.get("log", [])[:20]],
        dashboard=dashboard_info(),
        schedule_warnings=schedule_warnings(tasks),
    )


def schedule_warnings(tasks: dict[str, dict], plan: list[dict] | None = None) -> list[str]:
    """A line for each task whose next run isn't at its schedule.json time
    (New York). Catches what happened on 2026-11-01: triggers stored with a
    fixed UTC offset ran an hour early after the daylight-saving change."""
    out = []
    for t in schedule() if plan is None else plan:
        info = tasks.get(t["name"])
        if not info or not info.get("next_at"):
            continue  # not installed in this copy, or disabled
        nxt = datetime.fromisoformat(info["next_at"])            # this PC's clock, no zone
        hh, mm = map(int, t["ny"].split(":"))
        due = datetime.combine(nxt.date(), time(hh, mm), ET).astimezone().replace(tzinfo=None)
        if abs((nxt - due).total_seconds()) > 60:
            out.append(f"{t['name']} runs at {_when(nxt).split(' ', 1)[1]}, should be {_when(due).split(' ', 1)[1]}")
    return out


def dashboard_info() -> dict:
    """Where the dashboard is and when it was last built: an open page
    reloads itself when `build` changes (e.g. the 8am rebuild, or the
    snapshot's), so a tab left open overnight shows the new day."""
    p = dashboard_path()
    if not p.exists():
        return {"url": None, "build": None}
    return {"url": "/" + p.as_posix(), "build": str(p.stat().st_mtime_ns)}


def apply(body: dict) -> tuple[dict, str]:
    """Set a mode from a button press, start whichever bot has to act, and
    return (new state, a message for the page)."""
    strategies = control.available()
    sid, mode, close = body.get("strategy"), body.get("mode"), bool(body.get("close", False))
    if mode not in control.MODES:
        raise ValueError(f"unknown mode {mode!r}")
    if sid == "all":
        current = control.read_all()["strategies"]
        ids = [s for s in strategies if current.get(s, {}).get("mode", control.RUN) != mode]
    elif sid in strategies:
        ids = [sid]
    else:
        raise ValueError(f"unknown strategy {sid!r}")
    for s in ids:
        control.set_mode(s, mode, close=close, by="dashboard")
        logger.info("%s -> %s%s", s, mode, " (close at market)" if close and mode == control.STOP else "")

    started = []
    if market_open() and ids:
        tasks = task_info(fresh=True)
        to_start = set()
        for s in ids:
            meta = strategies[s]
            if mode == control.RUN and meta["intraday"]:
                to_start.add(meta["task"])
            elif mode == control.STOP and close:
                to_start.add("StepStopTradingBot" if s in STOCK_BOTS else meta["task"])
        for task in sorted(to_start):
            if tasks.get(task, {}).get("state") in ("Ready",) and start_task(task):
                started.append(task)

    names = ", ".join(strategies[s]["name"] for s in ids) or "Nothing to change"
    what = {control.RUN: "running", control.PAUSE: "paused: no new trades",
            control.STOP: "stopped" + (", closing positions at market" if close else "")}[mode]
    msg = f"{names}: {what}" if ids else "Nothing to change"
    if started:
        msg += " · started " + ", ".join(started)
    return state_payload(), msg


# ---------------------------------------------------------------- pages

STANDALONE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bot controls</title>
<style>
:root { color-scheme: light; --page:#f9f9f7; --surface:#fcfcfb; --surface-2:#f3f2ee; --ink:#0b0b0b; --ink-2:#52514e;
  --muted:#6f6d68; --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,0.10); --accent:#2a78d6; }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark; --page:#0d0d0d; --surface:#1a1a19; --surface-2:#232321;
  --ink:#f0efec; --ink-2:#c3c2b7; --muted:#9a9891; --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,0.10); --accent:#3987e5; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1200px; margin: 0 auto; padding: 20px 16px; }
h1 { font-size: 18px; margin: 0 0 4px; } .meta { color: var(--muted); font-size: 12px; margin: 0 0 16px; }
a { color: var(--accent); }
</style></head><body><main>
<h1>Bot controls</h1><p class="meta">__META__</p>
__PANEL__
</main></body></html>"""


def panel_html() -> str:
    return PANEL.read_text(encoding="utf-8").replace("{{CONTROL_PORT}}", str(settings.control_port))


def dashboard_path() -> Path:
    return Path(settings.reports_dir) / "dashboard.html"


class Handler(BaseHTTPRequestHandler):
    server_version = "BotControl/1"

    def _allowed_hosts(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

    def _host_ok(self) -> bool:
        # A page on another site that resolves its own name to 127.0.0.1 (DNS
        # rebinding) still sends its own name as the Host: refuse it.
        if self.headers.get("Host") not in self._allowed_hosts():
            self._json(HTTPStatus.FORBIDDEN, {"error": "wrong host"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return
        path = unquote(urlsplit(self.path).path)
        if path == "/":
            target = "/" + dashboard_path().as_posix() if dashboard_path().exists() else "/controls"
            self._send(HTTPStatus.FOUND, b"", "text/plain", {"Location": target})
        elif path == "/controls":
            meta = (f'<a href="/{dashboard_path().as_posix()}">Open the dashboard</a>' if dashboard_path().exists()
                    else "The dashboard hasn't been built yet (8am, by the control server and the premarket snapshot).")
            html = STANDALONE.replace("__META__", meta).replace("__PANEL__", panel_html())
            self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/ping":  # "are you up, and is a page open?" — not itself a page checking in
            last = self.server.last_poll
            self._json(HTTPStatus.OK, {"since_poll": round(clock.monotonic() - last, 1) if last else None})
        elif path == "/api/state":
            self.server.last_poll = clock.monotonic()
            # ?light=1: a background tab only asking whether the dashboard was rebuilt
            light = "light=1" in urlsplit(self.path).query
            self._json(HTTPStatus.OK, {"dashboard": dashboard_info()} if light else state_payload())
        else:
            self._static(path)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def _static(self, path: str) -> None:
        rel = path.lstrip("/")
        root = Path.cwd().resolve()
        target = (root / rel).resolve()
        roots = [root / r for r in STATIC_ROOTS] + [(root / settings.reports_dir).resolve()]
        allowed = any(target.is_relative_to(r) for r in roots)
        if not allowed or not target.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        body = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".md":
            ctype = "text/plain"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(HTTPStatus.OK, body, ctype)

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            self._json(HTTPStatus.BAD_REQUEST, {"error": "bad body"})
            return
        raw = self.rfile.read(length) if length else b""  # read first: refusing unread breaks the reply on Windows
        if not self._host_ok():
            return
        if urlsplit(self.path).path != "/api/control":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        origin = self.headers.get("Origin", "")
        if origin not in {f"http://{h}" for h in self._allowed_hosts()}:
            self._json(HTTPStatus.FORBIDDEN, {"error": "requests must come from the dashboard page"})
            return
        if (self.headers.get("X-Bot-Control") != "1"
                or not self.headers.get("Content-Type", "").startswith("application/json")):
            self._json(HTTPStatus.FORBIDDEN, {"error": "missing headers"})
            return
        if not raw:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "bad body"})
            return
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("expected an object")
            payload, msg = apply(body)
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except Exception as exc:
            logger.exception("Control change failed")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        self._json(HTTPStatus.OK, {**payload, "message": msg})

    def log_message(self, fmt: str, *args) -> None:
        if "/api/state" not in (args[0] if args else ""):
            logger.info("%s %s", self.address_string(), fmt % args)


def make_server(port: int | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", settings.control_port if port is None else port), Handler)
    server.last_poll = 0.0  # when an open page last asked for /api/state (time.monotonic)
    return server


OPEN_PAGE_WAIT = 150.0  # seconds; a background tab checks once a minute, and browsers may delay that to the next minute


def page_checked_in(port: int, since: float) -> bool:
    """Has a dashboard page polled the server on `port` (another process)
    since `since` (time.monotonic)? Asks /api/ping, which doesn't count as a poll."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=3) as r:
            age = json.load(r).get("since_poll")
    except (OSError, ValueError):
        return False
    return age is not None and age <= clock.monotonic() - since


def open_dashboard(port: int, server: ThreadingHTTPServer | None = None, wait: float = OPEN_PAGE_WAIT,
                   remote: bool = False) -> None:
    """Rebuild the dashboard (blank for a new account until its first
    report), then open it
    in the browser through this server, unless a dashboard page is already
    open: that page checks in within about a minute, sees the new build and
    reloads itself, so no second tab is opened. remote=True: the server is
    another process already on `port` (e.g. one started from the desktop
    shortcut), asked over /api/ping."""
    started = clock.monotonic()
    url = f"http://127.0.0.1:{port}/"
    try:
        from tradingbot.dashboard import build
        logger.info("Dashboard rebuilt: %s", build())
    except Exception:
        logger.exception("Dashboard build failed — opening the last one")
    # The redirect to the dashboard keeps the #: it opens on the Bot controls
    # tab (or /controls, if there's no dashboard at all).
    url += "#controls"
    if server is not None or remote:
        while clock.monotonic() - started < wait:
            if (server.last_poll >= started) if server is not None else page_checked_in(port, started):
                logger.info("A dashboard page is already open — it refreshes itself; not opening another")
                return
            clock.sleep(1.0)
    import webbrowser
    webbrowser.open(url)


SERVICE_START, SERVICE_END = time(8, 0), time(18, 0)  # the BotControlServer task's hours (New York)


def in_service_hours(now: datetime) -> bool:
    """Monday-Friday 8am-6pm New York: when the scheduled server is meant to run."""
    return now.weekday() < 5 and SERVICE_START <= now.time() < SERVICE_END


def stop_when_idle(server: ThreadingHTTPServer, minutes: float, poll: float = 30.0, now=lambda: datetime.now(ET)) -> None:
    """Shut down after `minutes` with no dashboard page checking in, but never
    during the scheduled hours: a server started from the desktop shortcut
    (e.g. on a weekend) then simply stands in for the 8am one, and closes once
    the day is over and no tab is open."""
    started = clock.monotonic()
    while True:
        clock.sleep(poll)
        if in_service_hours(now()):
            continue
        if clock.monotonic() - max(started, server.last_poll) >= minutes * 60:
            logger.info("No dashboard page open for %g minutes — stopping the control server", minutes)
            server.shutdown()
            return


def stop_at(server: ThreadingHTTPServer, until: time, poll: float = 30.0) -> None:
    """Shut the server down at `until` (New York time)."""
    while datetime.now(ET).time() < until:
        clock.sleep(poll)
    logger.info("It's %s — stopping the control server for the day", until.strftime("%H:%M"))
    server.shutdown()


def _arg(name: str) -> str | None:
    args = sys.argv[1:]
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None


def main() -> None:
    until = datetime.strptime(_arg("--until"), "%H:%M").time() if _arg("--until") else None
    idle = float(_arg("--idle")) if _arg("--idle") else None
    want_open = "--open" in sys.argv[1:]
    wait = 0.0 if "--now" in sys.argv[1:] else OPEN_PAGE_WAIT  # --now: someone just asked for the page
    try:
        server = make_server()
    except OSError:
        # Already running (the 8am start and a logon start can overlap, or one
        # was started from the desktop shortcut): rebuild and open the page,
        # unless a dashboard tab is open and refreshes itself.
        logger.info("Control server already running on port %d", settings.control_port)
        if want_open:
            open_dashboard(settings.control_port, wait=wait, remote=True)
        return
    port = server.server_address[1]
    logger.info("Control server on http://127.0.0.1:%d/ (strategies: %s)%s%s", port, ", ".join(control.available()),
                f", stopping at {until:%H:%M}" if until else "",
                f", stopping after {idle:g} idle minutes outside Mon-Fri 8am-6pm" if idle else "")
    if want_open:
        threading.Thread(target=open_dashboard, args=(port, server, wait), daemon=True).start()
    if until:
        threading.Thread(target=stop_at, args=(server, until), daemon=True).start()
    if idle:
        threading.Thread(target=stop_when_idle, args=(server, idle), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
