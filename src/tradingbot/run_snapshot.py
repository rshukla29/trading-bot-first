"""Daily pre-market / post-market account snapshot, plus week-to-date and
month-to-date period reports (generated alongside the postmarket run):
open orders, per-position unrealized P&L, closed-position realized P&L
for the period, and account-level P&L. Writes local HTML reports under
reports_dir and commits them to git (no push) so the account history
accumulates in version control.

Every run persists today's closed trades to the journal's closed_trades
table (see journal.py) — this is the *only* record of them once the day
passes: IBKR's reqExecutions() only returns today's executions regardless
of the requested start time (confirmed live: a filter asking for
everything since 2026-09-01 still returned only same-day fills), so a
weekly/monthly report can't reconstruct earlier closes by re-querying
IBKR after the fact. Period reports read from the journal, not IBKR.

Read-only — never places orders.

    python -m tradingbot.run_snapshot premarket
    python -m tradingbot.run_snapshot postmarket
"""
from __future__ import annotations

import calendar
import logging
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape
from pathlib import Path

from ib_async import IB, ExecutionFilter, Fill

from tradingbot.config import settings
from tradingbot.ibkr.client import IBKRClient
from tradingbot.journal import Journal
from tradingbot.paths import dated_dir
from tradingbot.protection import orphan_stops, working_stop_qty_by_symbol

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_STYLE = """
body { font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 24px; color: #1a1a1a; }
h1 { font-size: 18px; } h2 { font-size: 15px; margin-top: 28px; }
table { border-collapse: collapse; width: 100%; max-width: 800px; }
th, td { border: 1px solid #ddd; padding: 6px 10px; text-align: right; font-size: 13px; }
th { background: #f4f4f4; text-align: right; }
th:first-child, td:first-child { text-align: left; }
.pos { color: #0a7a2f; } .neg { color: #b3261e; }
.empty { color: #888; font-style: italic; }
.warn { color: #b3261e; font-weight: bold; }
"""


@dataclass
class ClosedTrade:
    symbol: str
    side: str
    quantity: float
    price: float
    realized_pnl: float


def _describe_contract(c) -> str:
    if c.secType == "OPT":
        return f"{c.symbol} {c.lastTradeDateOrContractMonth} {c.strike}{c.right}"
    if c.secType == "BAG":
        return f"{c.symbol} (combo)"
    return c.symbol


def _pnl_class(value: float) -> str:
    return "pos" if value >= 0 else "neg"


def get_closed_fills_today(ib: IB, today: date) -> list[Fill]:
    """Today's fills whose commission report shows a nonzero realizedPNL
    — IBKR attributes the whole round-trip's realized P&L to the closing
    fill, so an opening fill always reports 0.0 here.

    reqExecutions()'s own return value is unreliable for this: commission
    reports arrive asynchronously via a separate event shortly after the
    fill, so realizedPNL often reads back as 0.0 even on a genuine closing
    trade if read immediately. Waiting briefly and re-reading from
    ib.fills() (the live-updating cache) gets the real number — confirmed
    live: a same-day SPY option round-trip read realizedPNL=0.0 from the
    immediate return, then 418.06 (the correct value) after this wait.
    """
    today_str = today.strftime("%Y%m%d") + "-00:00:00"
    ib.reqExecutions(ExecutionFilter(time=today_str))
    ib.sleep(2)
    return [f for f in ib.fills() if f.commissionReport and f.commissionReport.realizedPNL != 0.0]


def persist_closed_fills(journal: Journal, fills: list[Fill], today: date) -> list[ClosedTrade]:
    """Records each fill in the journal (safe to call every run — dedup is
    by IBKR's own exec_id) and returns the normalized list for immediate
    use in today's report."""
    trades = []
    for f in fills:
        trade = ClosedTrade(
            symbol=_describe_contract(f.contract),
            side=f.execution.side,
            quantity=f.execution.shares,
            price=f.execution.price,
            realized_pnl=f.commissionReport.realizedPNL,
        )
        journal.record_closed_trade(
            f.execution.execId, today.isoformat(), trade.symbol, trade.side, trade.quantity, trade.price,
            trade.realized_pnl,
        )
        trades.append(trade)
    return trades


def _closed_trades_from_journal_rows(rows: list[sqlite3.Row]) -> list[ClosedTrade]:
    return [
        ClosedTrade(row["symbol"], row["side"], row["quantity"], row["price"], row["realized_pnl"])
        for row in rows
    ]


def _render_positions_section(portfolio) -> tuple[list[str], float]:
    parts: list[str] = ["<h2>Open positions &amp; unrealized P&amp;L</h2>"]
    if not portfolio:
        parts.append("<p class='empty'>(none)</p>")
        return parts, 0.0

    parts.append(
        "<table><tr><th>Symbol</th><th>Qty</th><th>Avg Cost</th><th>Mkt Price</th>"
        "<th>Mkt Value</th><th>Unrealized P&amp;L</th></tr>"
    )
    total_unrealized = 0.0
    for p in portfolio:
        desc = _describe_contract(p.contract)
        total_unrealized += p.unrealizedPNL
        parts.append(
            f"<tr><td>{escape(desc)}</td><td>{p.position:.0f}</td>"
            f"<td>{p.averageCost:.2f}</td><td>{p.marketPrice:.2f}</td>"
            f"<td>{p.marketValue:.2f}</td>"
            f"<td class='{_pnl_class(p.unrealizedPNL)}'>{p.unrealizedPNL:.2f}</td></tr>"
        )
    parts.append(
        f"<tr><td colspan='5'><b>TOTAL</b></td>"
        f"<td class='{_pnl_class(total_unrealized)}'><b>{total_unrealized:.2f}</b></td></tr>"
    )
    parts.append("</table>")
    return parts, total_unrealized


def stock_owners(journal: Journal) -> dict[str, str]:
    """symbol -> which bot owns that stock position (absent = manual)."""
    owners = {row["symbol"]: "trend_join" for row in journal.open_trend_join_positions()}
    for strategy in ("ma_crossover", "rsi2"):
        owners.update({s: strategy for s in journal.owned_stocks(strategy)})
    return owners


def _render_stop_coverage_section(portfolio, orders, owners: dict[str, str]) -> list[str]:
    """Every stock position, its owner and its working stop — so a bot
    position without a stop (the 2026-09-25 expired-DAY-stop failure) is
    visible in every snapshot instead of going unnoticed for days."""
    parts: list[str] = ["<h2>Stop-loss coverage (stocks)</h2>"]
    stocks = [p for p in portfolio if p.contract.secType == "STK"]
    if not stocks and not orphan_stops(orders, set()):
        parts.append("<p class='empty'>(no stock positions)</p>")
        return parts

    stop_qty = working_stop_qty_by_symbol(orders)
    stop_px: dict[str, float] = {}
    for t in orders:
        if t.contract.secType == "STK" and t.order.action == "SELL" and t.order.orderType == "STP":
            stop_px[t.contract.symbol] = t.order.auxPrice

    unprotected = []
    parts.append(
        "<table><tr><th>Symbol</th><th>Owner</th><th>Qty</th><th>Stop Qty</th>"
        "<th>Stop Price</th><th>Status</th></tr>"
    )
    for p in stocks:
        sym = p.contract.symbol
        owner = owners.get(sym, "manual")
        covered = stop_qty.get(sym, 0.0)
        if owner == "manual":
            status = "manual — not managed by bots"
        elif covered >= p.position:
            status = "protected"
        else:
            status = "<span class='warn'>UNPROTECTED</span>"
            unprotected.append(sym)
        price = f"{stop_px[sym]:.2f}" if sym in stop_px else "—"
        parts.append(
            f"<tr><td>{escape(sym)}</td><td>{escape(owner)}</td><td>{p.position:.0f}</td>"
            f"<td>{covered:.0f}</td><td>{price}</td><td>{status}</td></tr>"
        )
    parts.append("</table>")
    held = {p.contract.symbol for p in stocks if p.position > 0}
    orphans = orphan_stops(orders, held)
    if orphans:
        names = ", ".join(f"{t.contract.symbol} SELL {t.order.totalQuantity:.0f} @ {t.order.auxPrice:.2f}" for t in orphans)
        parts.insert(1, f"<p class='warn'>WARNING: stop orders with no position behind them (would open a short "
                        f"if triggered): {escape(names)}. Cancel them.</p>")
    if unprotected:
        parts.insert(1, f"<p class='warn'>WARNING: bot positions without a working stop: {escape(', '.join(unprotected))}. "
                        "The next MA/RSI(2) run will re-place the stop (or sell if already below it).</p>")
    return parts


def _render_closed_trades_section(trades: list[ClosedTrade], period_label: str) -> tuple[list[str], float]:
    parts: list[str] = [f"<h2>Closed positions P&amp;L ({escape(period_label)})</h2>"]
    if not trades:
        parts.append("<p class='empty'>(none)</p>")
        return parts, 0.0

    parts.append(
        "<table><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Fill Price</th>"
        "<th>Realized P&amp;L</th></tr>"
    )
    total_realized = 0.0
    for t in trades:
        total_realized += t.realized_pnl
        parts.append(
            f"<tr><td>{escape(t.symbol)}</td><td>{escape(t.side)}</td>"
            f"<td>{t.quantity:.0f}</td><td>{t.price:.2f}</td>"
            f"<td class='{_pnl_class(t.realized_pnl)}'>{t.realized_pnl:.2f}</td></tr>"
        )
    parts.append(
        f"<tr><td colspan='4'><b>TOTAL</b></td>"
        f"<td class='{_pnl_class(total_realized)}'><b>{total_realized:.2f}</b></td></tr>"
    )
    parts.append("</table>")
    return parts, total_realized


def build_report(
    client: IBKRClient, label: str, closed_trades: list[ClosedTrade], owners: dict[str, str] | None = None,
) -> str:
    ib = client.ib
    accounts = ib.managedAccounts()
    account = accounts[0] if accounts else ""

    orders = ib.reqAllOpenOrders()
    portfolio = [p for p in ib.portfolio() if p.position != 0]

    pnl = ib.reqPnL(account)
    ib.sleep(2)
    ib.cancelPnL(account)

    parts: list[str] = [
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Snapshot {label} {date.today().isoformat()}</title>"
        f"<style>{_STYLE}</style></head><body>",
        f"<h1>Trading bot snapshot: {escape(label)} — {date.today().isoformat()} "
        f"(account {escape(account)})</h1>",
    ]

    parts.append("<h2>Open orders</h2>")
    if not orders:
        parts.append("<p class='empty'>(none)</p>")
    else:
        parts.append(
            "<table><tr><th>Symbol</th><th>Action</th><th>Qty</th><th>Type</th>"
            "<th>Price</th><th>Status</th></tr>"
        )
        for t in orders:
            desc = _describe_contract(t.contract)
            price = t.order.lmtPrice or t.order.auxPrice
            parts.append(
                f"<tr><td>{escape(desc)}</td><td>{escape(t.order.action)}</td>"
                f"<td>{t.order.totalQuantity:.0f}</td><td>{escape(t.order.orderType)}</td>"
                f"<td>{price:.2f}</td><td>{escape(t.orderStatus.status)}</td></tr>"
            )
        parts.append("</table>")

    positions_parts, _ = _render_positions_section(portfolio)
    parts.extend(positions_parts)

    parts.extend(_render_stop_coverage_section(portfolio, orders, owners or {}))

    closed_parts, _ = _render_closed_trades_section(closed_trades, "today")
    parts.extend(closed_parts)

    net_liq = client.net_liquidation()
    ytd_pnl = net_liq - settings.account_starting_balance
    ytd_pct = (ytd_pnl / settings.account_starting_balance * 100) if settings.account_starting_balance else 0.0

    parts.append("<h2>Account P&amp;L (today)</h2>")
    parts.append(
        "<table>"
        f"<tr><td>Realized P&amp;L</td><td class='{_pnl_class(pnl.realizedPnL)}'>{pnl.realizedPnL:.2f}</td></tr>"
        f"<tr><td>Unrealized P&amp;L</td><td class='{_pnl_class(pnl.unrealizedPnL)}'>{pnl.unrealizedPnL:.2f}</td></tr>"
        f"<tr><td>Daily P&amp;L</td><td class='{_pnl_class(pnl.dailyPnL)}'>{pnl.dailyPnL:.2f}</td></tr>"
        "</table>"
    )

    parts.append("<h2>Overall</h2>")
    parts.append(
        "<table>"
        f"<tr><td>Total Balance (Net Liquidation)</td><td>{net_liq:,.2f}</td></tr>"
        f"<tr><td>YTD P&amp;L (vs {settings.account_starting_balance:,.0f} baseline)</td>"
        f"<td class='{_pnl_class(ytd_pnl)}'>{ytd_pnl:,.2f} ({ytd_pct:+.2f}%)</td></tr>"
        "</table>"
    )

    parts.append("</body></html>")
    return "\n".join(parts)


def build_period_report(client: IBKRClient, journal: Journal, period_label: str, period_start: date) -> str:
    """Week-to-date ("weekly", Monday through today) or month-to-date
    ("monthly", the 1st through today) report. On the last trading day of
    the period this doubles as the final summary for that week/month —
    no separate "closing" report needed. Closed trades come from the
    journal (see module docstring), not a live IBKR query, since IBKR's
    execution history doesn't actually reach back before today."""
    ib = client.ib
    accounts = ib.managedAccounts()
    account = accounts[0] if accounts else ""
    today = date.today()

    portfolio = [p for p in ib.portfolio() if p.position != 0]
    closed_trades = _closed_trades_from_journal_rows(
        journal.closed_trades_between(period_start.isoformat(), today.isoformat())
    )
    net_liq = client.net_liquidation()
    baseline = journal.balance_before(period_start.isoformat())

    parts: list[str] = [
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{period_label.capitalize()} report {today.isoformat()}</title>"
        f"<style>{_STYLE}</style></head><body>",
        f"<h1>Trading bot {escape(period_label)} report — {period_start.isoformat()} to "
        f"{today.isoformat()} (account {escape(account)})</h1>",
    ]

    positions_parts, unrealized_now = _render_positions_section(portfolio)
    parts.extend(positions_parts)

    closed_parts, realized_period = _render_closed_trades_section(closed_trades, period_label)
    parts.extend(closed_parts)

    total_period_pnl = realized_period + unrealized_now

    parts.append(f"<h2>Account P&amp;L ({escape(period_label)})</h2>")
    parts.append(
        "<table>"
        f"<tr><td>Realized P&amp;L</td><td class='{_pnl_class(realized_period)}'>{realized_period:.2f}</td></tr>"
        f"<tr><td>Unrealized P&amp;L (current)</td><td class='{_pnl_class(unrealized_now)}'>{unrealized_now:.2f}</td></tr>"
        f"<tr><td>Total P&amp;L</td><td class='{_pnl_class(total_period_pnl)}'>{total_period_pnl:.2f}</td></tr>"
        "</table>"
    )

    parts.append("<h2>Overall</h2>")
    if baseline is None:
        parts.append(
            "<table><tr><td>Total Balance (Net Liquidation)</td><td>"
            f"{net_liq:,.2f}</td></tr>"
            "<tr><td colspan='2' class='empty'>No prior balance recorded before this period yet — "
            "period % change unavailable until at least one full day has been recorded.</td></tr></table>"
        )
    else:
        balance_pnl = net_liq - baseline
        balance_pct = (balance_pnl / baseline * 100) if baseline else 0.0
        parts.append(
            "<table>"
            f"<tr><td>Total Balance (Net Liquidation)</td><td>{net_liq:,.2f}</td></tr>"
            f"<tr><td>{escape(period_label.capitalize())} P&amp;L (vs {baseline:,.2f} balance "
            f"on {period_start.isoformat()})</td>"
            f"<td class='{_pnl_class(balance_pnl)}'>{balance_pnl:,.2f} ({balance_pct:+.2f}%)</td></tr>"
            "</table>"
        )

    parts.append("</body></html>")
    return "\n".join(parts)


def report_dir(base: Path, day: date, per_day: bool) -> Path:
    """Year/month/day tree: daily snapshots in reports/YYYY/MM/DD/,
    weekly and monthly reports one level up in reports/YYYY/MM/."""
    return dated_dir(base, day, per_day)


def write_report_file(report: str, filename_stem: str, day: date, per_day: bool = True) -> Path:
    directory = report_dir(Path(settings.reports_dir), day, per_day)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{filename_stem}.html"
    path.write_text(report, encoding="utf-8")
    return path


def commit_report(path: Path, description: str) -> None:
    """Commit the report locally (no push) so the account history
    accumulates in version control without needing manual intervention."""
    try:
        subprocess.run(["git", "add", str(path)], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "commit", "-m", f"Snapshot: {description}"],
            check=True, capture_output=True, text=True,
        )
        logger.info("Committed %s", path)
    except subprocess.CalledProcessError as e:
        # Most common cause: nothing changed (re-run same day), not a real failure.
        logger.warning("git commit skipped/failed for %s: %s", path, e.stderr.strip())


def _week_start(today: date) -> date:
    return today - timedelta(days=today.weekday())  # Monday


def _month_start(today: date) -> date:
    return today.replace(day=1)


def _is_friday(today: date) -> bool:
    return today.weekday() == 4


def _is_last_trading_day_of_month(today: date) -> bool:
    """True on the last weekday (Mon-Fri) on or before the month's actual
    last calendar day — not simply "today's the 28th-31st", since when
    the calendar's last day falls on a weekend the real last trading day
    is the Friday before it. Doesn't know about market holidays (nothing
    else in this system does either — schtasks itself just fires Mon-Fri)
    so a holiday-shortened month's true last session could differ."""
    last_day_num = calendar.monthrange(today.year, today.month)[1]
    last_day = today.replace(day=last_day_num)
    while last_day.weekday() >= 5:  # Saturday=5, Sunday=6
        last_day -= timedelta(days=1)
    return today == last_day


def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    if label not in ("premarket", "postmarket"):
        raise SystemExit("usage: python -m tradingbot.run_snapshot [premarket|postmarket]")

    journal = Journal(settings.db_path)
    today = date.today()

    with IBKRClient(settings, client_id=settings.snapshot_client_id) as client:
        fills = get_closed_fills_today(client.ib, today)
        closed_trades_today = persist_closed_fills(journal, fills, today)

        report = build_report(client, label, closed_trades_today, stock_owners(journal))
        path = write_report_file(report, f"snapshot_{today.isoformat()}_{label}", today)
        print(f"Saved to {path}")
        commit_report(path, f"{label} {today.isoformat()}")

        if label == "postmarket":
            # Recorded every postmarket day regardless of period boundaries
            # — this is the running history weekly/monthly baselines are
            # read from later, not something itself gated to Friday/month-end.
            journal.record_daily_balance(today.isoformat(), client.net_liquidation())

            if _is_friday(today):
                weekly_report = build_period_report(client, journal, "weekly", _week_start(today))
                weekly_path = write_report_file(weekly_report, f"weekly_{today.isoformat()}", today, per_day=False)
                print(f"Saved to {weekly_path}")
                commit_report(weekly_path, f"weekly {today.isoformat()}")

            if _is_last_trading_day_of_month(today):
                monthly_report = build_period_report(client, journal, "monthly", _month_start(today))
                monthly_path = write_report_file(monthly_report, f"monthly_{today.isoformat()}", today, per_day=False)
                print(f"Saved to {monthly_path}")
                commit_report(monthly_path, f"monthly {today.isoformat()}")


if __name__ == "__main__":
    main()
