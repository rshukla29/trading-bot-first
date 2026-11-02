"""The dashboard: one self-contained HTML page with everything an owner
wants to see — account value and P&L, each strategy's record, every
closed trade, open positions, Stock ORB's Time Machine checks, links to
every snapshot / weekly / monthly report, the daily reviews, and which
bots ran each day.

Read-only and offline: it reads the journal, the reports folder, the
analysis folder and the logs; it never connects to IBKR. The data is
embedded in the page (dashboard_page.html is the template), so the file
opens by double-clicking, with no server.

    python -m tradingbot.run_dashboard          # writes reports/dashboard.html

Rebuilt automatically by the postmarket snapshot and the daily review.
Realized P&L by strategy is the same as the reports' (strategy_pnl.py);
tests/test_dashboard.py checks that.
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from tradingbot.journal import closed_trade_multiplier, closed_trade_prices
from tradingbot.strategy_pnl import ET, JOURNAL_STRATEGIES, STOCK_BOTS, _et_date, _stock_owner, _table_exists

TEMPLATE = Path(__file__).with_name("dashboard_page.html")
STRATEGIES = [n for n, _, _ in JOURNAL_STRATEGIES] + list(STOCK_BOTS.values()) + ["Manual / other"]
HEALTH_DAYS = 15


def _rows(conn, sql: str, args=()) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(sql, args).fetchall()


def _num(x, nd=2):
    return None if x is None else round(float(x), nd)


# ---------------------------------------------------------------- trades

def closed_trades(conn) -> list[dict]:
    """Every closed trade, one row each, newest first. Same sources and
    attribution as strategy_pnl: the bots' own journals, then IBKR's closed
    stock fills (one trade per stock per day) for MA-crossover, RSI(2) and
    anything manual."""
    out: list[dict] = []

    def add(strategy, ts_open, ts_close, symbol, qty, entry, exit_, pnl, reason):
        out.append(dict(date=_et_date(ts_close).isoformat(), opened=_et_date(ts_open).isoformat() if ts_open else None,
                        strategy=strategy, symbol=symbol, qty=_num(qty, 4), entry=_num(entry, 4),
                        exit=_num(exit_, 4), pnl=_num(pnl or 0.0), reason=reason or ""))

    if _table_exists(conn, "orb_positions"):
        for r in _rows(conn, "SELECT * FROM orb_positions WHERE status='closed' AND exit_date IS NOT NULL"):
            add("ORB (SPY 0DTE options)", r["entry_date"], r["exit_date"],
                f"{r['symbol']} {r['strike']:g}{r['right']} {r['expiry']}", r["contracts"], r["entry_premium"],
                r["exit_premium"], r["realized_pnl"], r["exit_reason"])
    if _table_exists(conn, "credit_orb_positions"):
        for r in _rows(conn, "SELECT * FROM credit_orb_positions WHERE status='closed' AND exit_ts IS NOT NULL"):
            add("ORB credit spreads", r["entry_ts"], r["exit_ts"],
                f"{r['symbol']} {r['short_strike']:g}/{r['long_strike']:g}{r['right']} spread", r["contracts"],
                r["credit"], r["exit_cost"], r["realized_pnl"], r["exit_reason"])
    if _table_exists(conn, "condor_positions"):
        for r in _rows(conn, "SELECT * FROM condor_positions WHERE status='closed' AND exit_date IS NOT NULL"):
            add("Iron condor", r["entry_date"], r["exit_date"], _condor_name(r), r["contracts"],
                r["credit_received"], r["exit_cost"], r["realized_pnl"], r["exit_reason"])
    if _table_exists(conn, "trend_join_positions"):
        for r in _rows(conn, "SELECT * FROM trend_join_positions WHERE status='closed' AND exit_ts IS NOT NULL"):
            qty = r["qty"] or 0
            avg_exit = r["entry_price"] + (r["realized_pnl"] or 0) / qty if qty else None
            add("Trend Join", r["entry_ts"], r["exit_ts"], r["symbol"], qty, r["entry_price"], avg_exit,
                r["realized_pnl"], r["exit_reason"])
    if _table_exists(conn, "stock_orb_positions"):
        for r in _rows(conn, "SELECT * FROM stock_orb_positions WHERE status='closed' AND exit_ts IS NOT NULL"):
            add("Stock ORB", r["entry_ts"], r["exit_ts"], r["symbol"], r["qty"], r["entry_price"], r["exit_price"],
                r["realized_pnl"], r["exit_reason"])

    journal_days = {(t["symbol"], t["date"]) for t in out if t["strategy"] in ("Trend Join", "Stock ORB")}
    if _table_exists(conn, "closed_trades"):
        sales: dict[tuple[str, str], dict] = {}
        for r in _rows(conn, "SELECT * FROM closed_trades WHERE symbol NOT LIKE '% %' ORDER BY ts"):
            key = (r["symbol"], r["date"])
            if key in journal_days:
                continue
            bought, sold = closed_trade_prices(r["side"], r["quantity"], r["price"], r["realized_pnl"],
                                               r["commission"], closed_trade_multiplier(r["symbol"]))
            s = sales.setdefault(key, dict(qty=0.0, cost=0.0, proceeds=0.0, pnl=0.0))
            s["qty"] += r["quantity"]
            s["cost"] += bought * r["quantity"]
            s["proceeds"] += sold * r["quantity"]
            s["pnl"] += r["realized_pnl"]
        for (sym, d), s in sales.items():
            owner = _stock_owner(conn, sym, date.fromisoformat(d))
            q = s["qty"] or 1
            out.append(dict(date=d, opened=None, strategy=owner, symbol=sym, qty=_num(s["qty"], 4),
                            entry=_num(s["cost"] / q, 4), exit=_num(s["proceeds"] / q, 4), pnl=_num(s["pnl"]),
                            reason="sold"))
    out.sort(key=lambda t: (t["date"], t["strategy"], t["symbol"]), reverse=True)
    return out


def _condor_name(r) -> str:
    return (f"{r['symbol']} {r['put_long_strike']:g}/{r['put_short_strike']:g}P "
            f"{r['call_short_strike']:g}/{r['call_long_strike']:g}C {r['expiry']}")


def open_positions(conn) -> list[dict]:
    """What the journals say is open. Live prices are in the latest snapshot."""
    out = []
    if _table_exists(conn, "condor_positions"):
        for r in _rows(conn, "SELECT * FROM condor_positions WHERE status='open'"):
            out.append(dict(strategy="Iron condor", symbol=_condor_name(r), opened=_et_date(r["entry_date"]).isoformat(),
                            qty=r["contracts"], entry=_num(r["credit_received"], 4),
                            note=f"credit {r['credit_received']:.2f} x {r['contracts']}, expires {r['expiry']}"))
    for strategy, table, ts, entry in (("ORB (SPY 0DTE options)", "orb_positions", "entry_date", "entry_premium"),
                                       ("ORB credit spreads", "credit_orb_positions", "entry_ts", "credit"),
                                       ("Trend Join", "trend_join_positions", "entry_ts", "entry_price"),
                                       ("Stock ORB", "stock_orb_positions", "entry_ts", "entry_price")):
        if not _table_exists(conn, table):
            continue
        for r in _rows(conn, f"SELECT * FROM {table} WHERE status='open'"):
            qty = r["contracts"] if "contracts" in r.keys() else r["qty"]
            out.append(dict(strategy=strategy, symbol=r["symbol"], opened=_et_date(r[ts]).isoformat(), qty=qty,
                            entry=_num(r[entry], 4), note=""))
    if _table_exists(conn, "stock_ownership"):
        names = {"rsi2": "RSI(2)", "ma_crossover": "MA-crossover"}
        for r in _rows(conn, "SELECT * FROM stock_ownership ORDER BY ts"):
            out.append(dict(strategy=names.get(r["strategy"], r["strategy"]), symbol=r["symbol"],
                            opened=_et_date(r["ts"]).isoformat(), qty=None, entry=None,
                            note="held; shares and price in the latest snapshot"))
    return out


def balances(conn) -> list[dict]:
    if not _table_exists(conn, "daily_balances"):
        return []
    return [dict(date=r["date"], value=_num(r["net_liquidation"]))
            for r in _rows(conn, "SELECT date, net_liquidation FROM daily_balances ORDER BY date")]


def tm_checks(conn) -> list[dict]:
    if not _table_exists(conn, "stock_orb_tm_checks"):
        return []
    rows = _rows(conn, """SELECT c.*, p.status AS pos_status, p.exit_reason, p.realized_pnl
                          FROM stock_orb_tm_checks c LEFT JOIN stock_orb_positions p ON p.id = c.position_id
                          ORDER BY c.id DESC""")
    return [dict(date=r["trade_date"], symbol=r["symbol"], bar=r["trigger_bar"], entry=r["entry"], stop=r["stop"],
                 target=r["target"], p_target=r["p_target"], p_stop=r["p_stop"], avg_r=r["avg_r"],
                 verdict=r["verdict"] or "", agrees=bool(r["agrees"]), decision=r["decision"],
                 pnl=_num(r["realized_pnl"]) if r["pos_status"] == "closed" else None) for r in rows]


# ---------------------------------------------------------------- files

_REPORT = re.compile(r"(snapshot|weekly|monthly)_(\d{4}-\d{2}-\d{2})(?:_(premarket|postmarket))?\.html$")


def report_files(reports_dir: Path, page_dir: Path) -> list[dict]:
    out = []
    for p in reports_dir.rglob("*.html"):
        m = _REPORT.search(p.name)
        if m:
            kind = m.group(3) or m.group(1)
            out.append(dict(date=m.group(2), kind=kind, href=_rel(p, page_dir)))
    out.sort(key=lambda r: (r["date"], r["kind"]), reverse=True)
    return out


def _rel(p: Path, base: Path) -> str:
    import os
    return Path(os.path.relpath(p.resolve(), base.resolve())).as_posix()


def reviews(analysis_dir: Path, page_dir: Path) -> list[dict]:
    out = []
    for p in (analysis_dir / "daily").rglob("review_*.md") if (analysis_dir / "daily").exists() else []:
        d = p.stem.removeprefix("review_")
        text = demojibake(p.read_text(encoding="utf-8-sig", errors="replace"))
        facts = p.with_name(f"facts_{d}.md")
        out.append(dict(date=d, html=markdown(text), summary=_summary(text), href=_rel(p, page_dir),
                        facts=_rel(facts, page_dir) if facts.exists() else None))
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


def studies(analysis_dir: Path, page_dir: Path) -> list[dict]:
    """One-off analyses: every folder in analysis/ other than daily/, with its README if any."""
    out = []
    if analysis_dir.exists():
        for d in sorted(analysis_dir.iterdir(), reverse=True):
            if d.is_dir() and d.name != "daily":
                readme = d / "README.md"
                out.append(dict(name=d.name, href=_rel(readme if readme.exists() else d, page_dir)))
    return out


def demojibake(text: str) -> str:
    """Repair UTF-8 that was read as code page 437 ("—" saved as "ΓÇö"),
    which the reviews saved before 2026-11-02 have. Each run of non-ASCII
    characters is re-decoded only if that works; anything else is kept."""
    def fix(m: re.Match) -> str:
        try:
            return m.group(0).encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return m.group(0)
    return re.sub(r"[^\x00-\x7f]+", fix, text)


def _summary(md: str) -> str:
    """The first paragraph under '## Summary' (or the first paragraph), as HTML."""
    m = re.search(r"^##\s+Summary\s*\n+(.+?)(?:\n\s*\n|\n#)", md, re.S | re.M)
    if not m:
        paras = [p for p in re.split(r"\n\s*\n", md) if p.strip() and not p.lstrip().startswith("#")]
        if not paras:
            return ""
        return _inline(paras[0].strip())
    return _inline(m.group(1).strip())


# Lines ib_async logs at ERROR level that are IBKR's own messages
# ("Error 202, reqId 8995: Order Canceled", 1100/1102 connection lost /
# restored, 162 / 10197 market data notices) or its request timeouts while
# reconnecting. They count as warnings; an error is a line the bots'
# own code logged at ERROR (usually with a traceback).
_IB_NOTICE = re.compile(r"ERROR (Error \d+, reqId|.*request timed out)")


def _strip_log(line: str) -> str:
    return re.sub(r"^(python\.exe : )?\d{4}-\d{2}-\d{2} [\d:,]+ ERROR ", "", line)


def bot_health(logs_dir: Path, today: date, days: int = HEALTH_DAYS) -> dict:
    """For the last `days` dates with any log: per bot, ran / warnings / errors."""
    by_day: dict[str, dict[str, dict]] = {}
    pat = re.compile(r"^(?P<bot>[a-z0-9_]+)_(?P<d>\d{4}-\d{2}-\d{2})\.log$")
    files = [(m, p) for p in logs_dir.glob("*.log") if (m := pat.match(p.name))] if logs_dir.exists() else []
    dates = sorted({m["d"] for m, _ in files if m["d"] <= today.isoformat()}, reverse=True)[:days]
    keep = set(dates)
    for m, p in files:
        if m["d"] not in keep:
            continue
        lines = _read_log(p).splitlines()
        errors = [line for line in lines if " ERROR " in line and not _IB_NOTICE.search(line)]
        warnings = [line for line in lines if " WARNING " in line or (" ERROR " in line and _IB_NOTICE.search(line))]
        last = next((line for line in reversed(lines) if line.strip()), "")
        by_day.setdefault(m["d"], {})[m["bot"]] = dict(
            errors=len(errors), warnings=len(warnings), size=p.stat().st_size,
            first_error=_strip_log(errors[0])[:240] if errors else "", last=last[:240])
    bots = sorted({b for d in by_day.values() for b in d})
    return dict(dates=dates, bots=bots, cells=by_day)


def _read_log(p: Path) -> str:
    raw = p.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- markdown

def _inline(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)
    s = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<em>\1</em>", s)
    return re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)


def markdown(md: str) -> str:
    """The small Markdown subset the reviews use: headings, paragraphs,
    bullet / numbered lists (one level of nesting), tables, code blocks,
    bold, italics, inline code, links. Everything is HTML-escaped first."""
    out: list[str] = []
    lines = md.replace("\r\n", "\n").split("\n")
    i, para, lists = 0, [], []

    def flush():
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()

    def close_lists(depth=0):
        while len(lists) > depth:
            out.append(f"</li></{lists.pop()}>")

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush(); close_lists()
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            out.append("<pre><code>" + html.escape("\n".join(lines[i + 1:j])) + "</code></pre>")
            i = j + 1
            continue
        h = re.match(r"^(#{1,6})\s+(.*)", line)
        if h:
            flush(); close_lists()
            n = min(len(h.group(1)) + 1, 6)
            out.append(f"<h{n}>{_inline(h.group(2))}</h{n}>")
            i += 1
            continue
        if line.lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1]):
            flush(); close_lists()
            cells = lambda s: [c.strip() for c in s.strip().strip("|").split("|")]
            out.append("<div class='tablewrap'><table><thead><tr>" +
                       "".join(f"<th>{_inline(c)}</th>" for c in cells(line)) + "</tr></thead><tbody>")
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells(lines[i])) + "</tr>")
                i += 1
            out.append("</tbody></table></div>")
            continue
        li = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)", line)
        if li:
            flush()
            depth = 1 + (len(li.group(1).expandtabs(4)) >= 2)
            tag = "ol" if li.group(2)[0].isdigit() else "ul"
            if len(lists) < depth:
                while len(lists) < depth:
                    out.append(f"<{tag}><li>")
                    lists.append(tag)
            else:
                close_lists(depth)
                out.append("</li><li>")
            out.append(_inline(li.group(3)))
            i += 1
            continue
        if not line.strip():
            flush(); close_lists()
            i += 1
            continue
        if lists and line.startswith(" "):
            out.append(" " + _inline(line.strip()))
        elif re.match(r"^\s*(---|\*\*\*)\s*$", line):
            flush(); close_lists()
            out.append("<hr>")
        else:
            close_lists()
            para.append(line.strip())
        i += 1
    flush(); close_lists()
    return "\n".join(out)


# ---------------------------------------------------------------- page

def collect(db_path: str, reports_dir: Path, analysis_dir: Path, logs_dir: Path, page_dir: Path,
            starting_balance: float, today: date | None = None) -> dict:
    today = today or datetime.now(ET).date()
    conn = sqlite3.connect(db_path)
    try:
        data = dict(trades=closed_trades(conn), open=open_positions(conn), balances=balances(conn),
                    tm=tm_checks(conn))
    finally:
        conn.close()
    data.update(
        generated=datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"), today=today.isoformat(),
        starting_balance=starting_balance, strategies=STRATEGIES,
        reports=report_files(reports_dir, page_dir), reviews=reviews(analysis_dir, page_dir),
        studies=studies(analysis_dir, page_dir), health=bot_health(logs_dir, today),
    )
    return data


def render(data: dict) -> str:
    # Every "<" as <: nothing in the data can close the <script> it sits in.
    payload = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    return TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", payload)


def build(out_path: Path | None = None, today: date | None = None) -> Path:
    from tradingbot.config import settings
    reports_dir = Path(settings.reports_dir)
    out_path = out_path or reports_dir / "dashboard.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = collect(settings.db_path, reports_dir, Path("analysis"), Path("logs"), out_path.parent,
                   settings.account_starting_balance, today)
    out_path.write_text(render(data), encoding="utf-8")
    return out_path

