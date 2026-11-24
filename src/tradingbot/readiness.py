"""Readiness check: is everything the bots need in place right now?

Run by the control server at its 8am start and by the Run checks now
button on the Bot health tab (run_readiness.py does the work, in its own
process, so a slow IBKR connection never blocks the server). Results go to
data/readiness.json; the dashboard shows them live.

Each check: {"id", "group": "pc" | "ibkr", "name", "status": pass|warn|fail|skip,
"detail": what was found, "fix": what to do (warn/fail)}.

It never places orders: the API read-only / size-limit check uses IBKR's
what-if preview, which IBKR evaluates without sending anything to the market.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from tradingbot.config import settings

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "data" / "readiness.json"
ET = ZoneInfo("America/New_York")
PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
STALE_RUN_SECONDS = 180  # a "running" mark older than this was a crashed run
MARKET_OPEN, MARKET_CLOSE = time(9, 30), time(16, 0)

# IBKR error codes that mean "no (live) market data for this"
NO_DATA = {354: "not subscribed", 10089: "needs an additional subscription (delayed only)",
           10090: "part of the data isn't subscribed", 10091: "part of the data isn't subscribed",
           10167: "showing delayed data", 10168: "not subscribed, and delayed data isn't enabled",
           2186: "not subscribed for the paper account"}
COMPETING = 10197  # "No market data during competing live session"


def check(cid: str, group: str, name: str, status: str, detail: str = "", fix: str = "") -> dict:
    return {"id": cid, "group": group, "name": name, "status": status, "detail": detail,
            "fix": fix if status in (WARN, FAIL) else ""}


# ---------------------------------------------------------------- this PC

def check_tasks() -> dict:
    from tradingbot import run_control_server as srv
    expected = [t["name"] for t in srv.schedule() if (REPO / "scripts" / t["script"]).exists()]
    try:
        tasks = srv.task_info(fresh=True)
    except Exception as exc:
        return check("tasks", "pc", "Scheduled tasks", WARN, f"Couldn't read Task Scheduler: {exc}",
                     "Open Task Scheduler and check the bots' tasks exist")
    missing = [n for n in expected if tasks.get(n, {}).get("state", "Missing") == "Missing"]
    disabled = [n for n in expected if tasks.get(n, {}).get("state") == "Disabled"]
    off = srv.schedule_warnings({n: tasks[n] for n in expected if n in tasks})
    if missing:
        return check("tasks", "pc", "Scheduled tasks", FAIL, "Missing: " + ", ".join(missing),
                     "Run scripts\\install.ps1 again (right-click → Run with PowerShell)")
    if disabled:
        return check("tasks", "pc", "Scheduled tasks", FAIL, "Disabled: " + ", ".join(disabled),
                     "In Task Scheduler, right-click each one → Enable")
    if off:
        return check("tasks", "pc", "Scheduled tasks", FAIL, "; ".join(off),
                     "Run scripts\\register_tasks.ps1 (right-click → Run with PowerShell)")
    return check("tasks", "pc", "Scheduled tasks", PASS, f"All {len(expected)} present, enabled and on time")


def check_settings() -> dict:
    if settings.live_trading:
        return check("settings", "pc", "Safe settings", FAIL, "LIVE_TRADING=true in .env",
                     "Set LIVE_TRADING=false in .env: these bots are for paper trading")
    if settings.ibkr_port in (7496, 4001):
        return check("settings", "pc", "Safe settings", FAIL, f"IBKR_PORT={settings.ibkr_port} is a live-trading port",
                     "Set IBKR_PORT=7497 (TWS paper) in .env")
    return check("settings", "pc", "Safe settings", PASS, f"Paper trading only, port {settings.ibkr_port}")


def check_journal() -> dict:
    db = Path(settings.db_path)
    db = db if db.is_absolute() else REPO / db
    folder = db.parent
    if not folder.exists():
        return check("journal", "pc", "Trade diary", FAIL, f"Folder {folder} doesn't exist",
                     f"Check DB_PATH in .env (now {settings.db_path})")
    if not os.access(db if db.exists() else folder, os.W_OK):
        return check("journal", "pc", "Trade diary", FAIL, f"Can't write to {db}",
                     "Close any program that has the file open, or check the folder's permissions")
    return check("journal", "pc", "Trade diary", PASS,
                 f"{settings.db_path}" + ("" if db.exists() else " (created at the first trade)"))


def _powercfg_ac_minutes() -> int | None:
    """Minutes before the PC sleeps on mains power (0 = never); None if unknown."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    values = re.findall(r":\s*0x([0-9a-fA-F]+)\s*$", out, re.M)
    return int(values[-2], 16) // 60 if len(values) >= 2 else None  # AC index, then DC; in seconds


def check_sleep(minutes: int | None = None) -> dict:
    minutes = _powercfg_ac_minutes() if minutes is None else minutes
    if minutes is None:
        return check("sleep", "pc", "PC stays awake", SKIP, "Couldn't read the power settings")
    if minutes == 0:
        return check("sleep", "pc", "PC stays awake", PASS, "Never sleeps when plugged in")
    return check("sleep", "pc", "PC stays awake", WARN, f"Sleeps after {minutes} minutes without use (plugged in)",
                 "Settings → System → Power → When plugged in, put my device to sleep after: Never. "
                 "A sleeping PC stops the bots")


def check_claude() -> dict:
    if shutil.which("claude"):
        return check("claude", "pc", "Claude Code (daily review)", PASS, "Installed")
    return check("claude", "pc", "Claude Code (daily review)", WARN, "Not found",
                 "Optional: install Claude Code for the 4:50pm written review. Trading works without it")


def check_updates() -> dict:
    from tradingbot import updates
    s = updates.read_status()
    have = updates.current()
    if not s:
        return check("updates", "pc", "Version", PASS, f"v{have} (not checked for updates yet)")
    if s.get("error"):
        return check("updates", "pc", "Version", WARN, f"v{have}; the update check failed: {s['error'][:120]}",
                     "Check the internet connection; it tries again at the next start")
    p = updates.payload()
    if p["available"]:
        return check("updates", "pc", "Version", WARN, f"v{have}; v{p['latest']} is available",
                     "Install it with Update now on the Bot controls tab (after 5:15pm, at the weekend or before 8am)")
    return check("updates", "pc", "Version", PASS, f"v{have}, up to date")


def check_rules() -> dict:
    """The Rules tab's saved rules can be used (rules.py)."""
    from tradingbot import rules
    cap, vals, problems = rules.effective()
    if problems:
        return check("rules", "pc", "Trading rules", FAIL, "; ".join(problems)[:300],
                     "Open the Rules tab, fix or reset the rules named here, and save")
    acct = rules.account_value()
    if cap and acct and cap > acct:
        return check("rules", "pc", "Trading rules", WARN,
                     f"Capital ${cap:,.0f} is more than the account's ${acct:,.0f}; the bots use ${acct:,.0f}",
                     "Lower the capital on the Rules tab, or leave it blank for the whole account")
    base = rules.defaults()
    custom = [rules.SCHEMA[s]["name"] for s in rules.STRATEGIES if vals[s] != base[s]]
    detail = (f"Capital ${cap:,.0f}" if cap else "Whole account") + "; " + (
        "custom rules for " + ", ".join(custom) if custom else "default rules")
    return check("rules", "pc", "Trading rules", PASS, detail)


def pc_checks() -> list[dict]:
    out = []
    for fn in (check_settings, check_rules, check_journal, check_tasks, check_sleep, check_claude, check_updates):
        try:
            out.append(fn())
        except Exception as exc:  # one broken check never hides the others
            logger.exception("Check %s failed", fn.__name__)
            out.append(check(fn.__name__.removeprefix("check_"), "pc", fn.__name__, WARN, f"The check itself failed: {exc}", "See logs\\readiness_*.log"))
    return out


# ---------------------------------------------------------------- IBKR

def expected_account() -> str | None:
    """The DU… account this copy is set up for, from DB_PATH / REPORTS_DIR."""
    m = re.search(r"(DU[A-Z0-9]{4,})", f"{settings.db_path} {settings.reports_dir}")
    return m.group(1) if m else None


def market_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now(ET)
    return now.weekday() < 5 and MARKET_OPEN <= now.time() < MARKET_CLOSE


def data_status(name: str, codes: dict[int, str], got_prices: bool, delayed: bool, in_hours: bool) -> tuple[str, str]:
    """(status, detail) for one market data request from the IBKR error codes it drew."""
    bad = {c: m for c, m in codes.items() if c in NO_DATA}
    if bad:
        c, m = next(iter(bad.items()))
        return FAIL, f"Error {c}: {NO_DATA[c]} ({m[:90]})"
    if COMPETING in codes:
        return FAIL, f"Error {COMPETING}: no data while your real account is logged in elsewhere"
    if delayed:
        return FAIL, "Only delayed prices"
    if got_prices:
        return PASS, "Live prices" if in_hours else "No subscription errors; prices received"
    if in_hours:
        return WARN, "No prices received in a few seconds"
    return PASS, "No subscription errors (live prices are confirmed during market hours)"


def ibkr_checks(out: list[dict] | None = None, timeout: float = 10.0, wait_minutes: float = 0) -> list[dict]:
    """Appends to `out` as it goes, so a crash part-way keeps what was checked.
    wait_minutes: keep retrying while TWS isn't up yet (the 8am run)."""
    from ib_async import IB, Option, Stock
    from tradingbot.ibkr.client import connect_with_retry

    def skipped(why: str) -> list[dict]:
        return [check(cid, "ibkr", order_check_name() if cid == "orders" else name, SKIP, why) for cid, name in IBKR_LATER]

    errors: list[tuple[int, int, str]] = []
    ib = IB()
    ib.errorEvent += lambda req_id, code, msg, contract=None: errors.append((req_id, code, msg))
    out = [] if out is None else out
    try:
        connect_with_retry(ib, settings.ibkr_host, settings.ibkr_port, settings.readiness_client_id,
                           wait_minutes=wait_minutes, timeout=timeout)
    except Exception as exc:
        out.append(check("tws", "ibkr", "TWS connection", FAIL,
                         f"Can't connect to {settings.ibkr_host}:{settings.ibkr_port} ({type(exc).__name__})",
                         "Open TWS and log in to the paper account; in TWS: Edit → Global Configuration → API → Settings: "
                         "Enable ActiveX and Socket Clients, port 7497 (setup step 7)"))
        out += skipped("Needs the TWS connection")
        return out
    try:
        out.append(check("tws", "ibkr", "TWS connection", PASS, f"Connected on port {settings.ibkr_port}"))
        accounts = ib.managedAccounts()
        acct = accounts[0] if accounts else ""
        want = expected_account()
        if not acct.startswith("DU"):
            out.append(check("account", "ibkr", "Paper account", FAIL, f"Logged in to {acct or 'no account'}",
                             "Log TWS out and back in with your paper username (it starts with DU…)"))
            out += skipped("Needs a paper account")[1:]
            return out
        if want and acct != want:
            out.append(check("account", "ibkr", "Paper account", FAIL, f"Logged in to {acct}, but this copy is set up for {want}",
                             f"Log TWS in to {want}, or change DB_PATH / REPORTS_DIR in .env for the new account"))
        else:
            out.append(check("account", "ibkr", "Paper account", PASS, acct))

        in_hours = market_hours()
        ib.reqMarketDataType(1)

        def watch(contract, seconds: float = 3.0):
            start = len(errors)
            if not contract.conId:
                ib.qualifyContracts(contract)
            t = ib.reqMktData(contract, "", False, False)
            ib.sleep(seconds)
            got = any(v == v and v not in (None, -1, 0) for v in (t.bid, t.ask, t.last, t.close))
            delayed = t.marketDataType in (3, 4)
            ib.cancelMktData(contract)
            codes = {c: m for _, c, m in errors[start:]}
            return codes, got, delayed

        spy = Stock("SPY", "SMART", "USD")
        ib.qualifyContracts(spy)
        competing = False
        for cid, name, contract in (("data_spy", "SPY prices", spy),
                                    ("data_nasdaq", "Nasdaq stock prices (AAPL)", Stock("AAPL", "SMART", "USD", primaryExchange="NASDAQ")),
                                    ("data_nyse", "NYSE stock prices (JPM)", Stock("JPM", "SMART", "USD", primaryExchange="NYSE"))):
            codes, got, delayed = watch(contract)
            competing |= COMPETING in codes
            st, detail = data_status(name, codes, got, delayed, in_hours)
            out.append(check(cid, "ibkr", name, st, detail, DATA_FIX))

        opt = None
        try:
            chain = next(c for c in ib.reqSecDefOptParams("SPY", "", "STK", spy.conId) if c.exchange == "SMART")
            # IBKR can still list an expiry that has passed (e.g. last Friday's on a
            # Monday), which has no contract left: use the first one from today on.
            today = datetime.now(ET).strftime("%Y%m%d")
            exp = min(e for e in chain.expirations if e >= today)
            strikes = sorted(chain.strikes)
            low = [s for s in strikes if s <= strikes[len(strikes) // 2] * 0.8] or strikes
            opt = Option("SPY", exp, low[-1], "P", "SMART")
            ib.qualifyContracts(opt)
            codes, got, delayed = watch(opt)
            competing |= COMPETING in codes
            st, detail = data_status("SPY options", codes, got, delayed, in_hours)
            out.append(check("data_options", "ibkr", "SPY option prices", st, detail,
                             "Subscribe to OPRA (US options) in Client Portal and share it with the paper account (setup step 2)"))
        except Exception as exc:
            out.append(check("data_options", "ibkr", "SPY option prices", WARN, f"Couldn't look up an option: {exc}",
                             "Try again; if it keeps failing, check the OPRA subscription (setup step 2)"))

        start = len(errors)
        bars = ib.reqHistoricalData(spy, "", "1 D", "1 min", "TRADES", True, timeout=20)
        hist_codes = {c: m for _, c, m in errors[start:]}
        if bars:
            out.append(check("history", "ibkr", "Price history (1-minute bars)", PASS, f"{len(bars)} bars, last {bars[-1].date:%a %H:%M}"))
        else:
            code = next(iter(hist_codes), None)
            out.append(check("history", "ibkr", "Price history (1-minute bars)", FAIL,
                             f"No bars{f' (error {code}: {hist_codes[code][:90]})' if code else ''}",
                             "Check the market data subscriptions (setup step 2), then run the checks again"))

        out.append(check("competing", "ibkr", "Real account not logged in elsewhere",
                         FAIL if competing else PASS,
                         "IBKR reports a competing live session (error 10197)" if competing else "No competing session",
                         "Log out of your real account in TWS, IBKR Desktop and the IBKR mobile app; while it's logged in, "
                         "the paper account gets no live prices"))

        out.append(order_check(ib, opt, errors))
    finally:
        ib.disconnect()
    return out


DATA_FIX = ("In Client Portal: Settings → Market Data Subscriptions (NYSE, Nasdaq, OPRA, Non-Professional), and "
            "Settings → Paper Trading Account → share real-time market data. Then log TWS out and back in (setup step 2)")


def order_check(ib, opt, errors: list) -> dict:
    """Read-only API and the precautionary size limit, from a what-if preview
    of the iron condor's order size (never sent)."""
    from ib_async import LimitOrder
    size = order_size()
    name = order_check_name()
    if opt is None:
        return check("orders", "ibkr", name, SKIP, "Needs an option contract")
    start = len(errors)
    try:  # bounded: an order error can leave the preview unanswered
        state = ib.run(ib.whatIfOrderAsync(opt, LimitOrder("BUY", size, 0.01, tif="DAY")), timeout=10)
    except Exception as exc:
        state = None
        logger.info("what-if failed: %s", exc)
    codes = {c: m for _, c, m in errors[start:]}
    if any(c == 321 or "read-only" in m.lower() for c, m in codes.items()):
        return check("orders", "ibkr", name, FAIL, "The TWS API is read-only",
                     "TWS: Edit → Global Configuration → API → Settings → untick Read-Only API")
    if 383 in codes:
        return check("orders", "ibkr", name, FAIL, codes[383][:140],
                     "TWS: Global Configuration → Presets → Options (and Stocks) → Precautionary Settings → "
                     f"Size Limit at least {size}, or tick API → Settings → Bypass Order Precautions for API Orders")
    if state is None or not getattr(state, "initMarginChange", ""):
        code = next(iter(codes), None)
        return check("orders", "ibkr", name, WARN,
                     f"The order preview didn't answer{f' (error {code}: {codes[code][:90]})' if code else ''}",
                     f"Check TWS's API settings (setup step 7) and the order size limit (at least {size})")
    return check("orders", "ibkr", name, PASS,
                 f"Order preview accepted: API isn't read-only and {size} contracts aren't refused (nothing was sent)")


def order_size() -> int:
    """The largest option order the rules allow (at least 10): what TWS's
    order size limit has to accept."""
    from tradingbot import rules
    try:
        vals = rules.effective()[1]
    except Exception:
        return 10
    sizes = [10]
    for sid in ("orb", "orb_hp", "credit_orb", "iron_condor"):
        v = vals[sid]
        sizes.append(min(v["maxQty"], v["size"]["fixed"]) if v["size"]["mode"] == "fixed" else v["maxQty"])
    return int(max(sizes))


def order_check_name() -> str:
    return f"Orders allowed ({order_size()} contracts)"


IBKR_LATER = [("account", "Paper account"), ("data_spy", "SPY prices"), ("data_nasdaq", "Nasdaq stock prices (AAPL)"),
              ("data_nyse", "NYSE stock prices (JPM)"), ("data_options", "SPY option prices"),
              ("history", "Price history (1-minute bars)"), ("competing", "Real account not logged in elsewhere"),
              ("orders", "Orders allowed (10 contracts)")]  # the name follows order_size() when run


# ---------------------------------------------------------------- results

def summary(checks: list[dict]) -> dict:
    n = {s: sum(c["status"] == s for c in checks) for s in (PASS, WARN, FAIL, SKIP)}
    n["ready"] = n[FAIL] == 0 and n[SKIP] == 0
    return n


def _write(data: dict) -> None:
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    tmp = RESULTS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, RESULTS)


def read() -> dict:
    try:
        return json.loads(RESULTS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def running(data: dict | None = None, now: datetime | None = None) -> bool:
    data = read() if data is None else data
    if not data.get("running"):
        return False
    try:
        age = ((now or datetime.now(ET)) - datetime.fromisoformat(data["started_at"])).total_seconds()
    except (KeyError, ValueError, TypeError):
        return False
    return age < data.get("max_seconds", STALE_RUN_SECONDS)


def _running_mark(wait_minutes: float) -> dict:
    return {"running": True, "started_at": datetime.now(ET).isoformat(timespec="seconds"),
            "max_seconds": STALE_RUN_SECONDS + wait_minutes * 60, "waiting_for_tws": wait_minutes > 0}


def payload() -> dict:
    """What the dashboard shows."""
    d = read()
    checks = d.get("checks", [])
    return {"running": running(d), "waiting_for_tws": running(d) and bool(d.get("waiting_for_tws")),
            "started_at": d.get("started_at"), "finished_at": d.get("finished_at"),
            "checks": checks, "summary": summary(checks) if checks else None}


def run(notify: bool = False, wait_minutes: float = 0) -> dict:
    """Run every check (in this process) and save the results. wait_minutes:
    how long to wait for TWS to come up (the 8am run; 0 for Run checks now)."""
    old = read()
    mark = _running_mark(wait_minutes)
    started = mark["started_at"]
    _write({**old, **mark})
    checks: list[dict] = []
    try:
        checks = pc_checks()
        ibkr: list[dict] = []
        try:
            ibkr_checks(ibkr, wait_minutes=wait_minutes)
        except Exception as exc:
            logger.exception("IBKR checks failed")
            done = {c["id"] for c in ibkr}
            ibkr += [check(cid, "ibkr", name, SKIP, f"Not checked: the check stopped with an error ({str(exc)[:120]})")
                     for cid, name in [("tws", "TWS connection")] + IBKR_LATER if cid not in done]
        checks += ibkr
    finally:
        data = {"running": False, "started_at": started, "finished_at": datetime.now(ET).isoformat(timespec="seconds"),
                "checks": checks}
        _write(data)
    s = summary(checks)
    logger.info("Readiness: %d pass, %d warnings, %d failed, %d skipped", s[PASS], s[WARN], s[FAIL], s[SKIP])
    for c in checks:
        if c["status"] != PASS:
            logger.info("  %s %s: %s", c["status"].upper(), c["name"], c["detail"])
    if notify and (s[FAIL] or s[SKIP]):
        bad = [c["name"] for c in checks if c["status"] in (FAIL, SKIP)]
        from tradingbot.notify import windows_notify
        windows_notify("Trading bots: not ready",
                       f"{len(bad)} problem{'s' if len(bad) > 1 else ''}: {', '.join(bad[:3])}{'…' if len(bad) > 3 else ''}. "
                       "Open the dashboard's Bot health tab.")
    return data


def start(notify: bool = False, wait_minutes: float = 0) -> bool:
    """Run the checks in the background (run_readiness.py). False if already running."""
    if running():
        return False
    _write({**read(), **_running_mark(wait_minutes)})
    log_dir = REPO / "logs"
    log_dir.mkdir(exist_ok=True)
    log = open(log_dir / f"readiness_{datetime.now(ET):%Y-%m-%d}.log", "ab")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    subprocess.Popen([sys.executable, "-m", "tradingbot.run_readiness", *(["--notify"] if notify else []),
                      *(["--wait", f"{wait_minutes:g}"] if wait_minutes else [])],
                     cwd=str(REPO), stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=flags)
    return True
