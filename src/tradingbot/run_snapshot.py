"""Daily pre-market / post-market account snapshot: open orders,
per-position unrealized P&L, and account-level realized/unrealized/daily
P&L. Writes a local HTML report under reports_dir and commits it to git
(no push) so the account history accumulates in version control.

Read-only — never places orders.

    python -m tradingbot.run_snapshot premarket
    python -m tradingbot.run_snapshot postmarket
"""
from __future__ import annotations

import logging
import subprocess
import sys
from datetime import date
from html import escape
from pathlib import Path

from tradingbot.config import settings
from tradingbot.ibkr.client import IBKRClient

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
"""


def _describe_contract(c) -> str:
    if c.secType == "OPT":
        return f"{c.symbol} {c.lastTradeDateOrContractMonth} {c.strike}{c.right}"
    if c.secType == "BAG":
        return f"{c.symbol} (combo)"
    return c.symbol


def _pnl_class(value: float) -> str:
    return "pos" if value >= 0 else "neg"


def build_report(client: IBKRClient, label: str) -> str:
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

    parts.append("<h2>Open positions &amp; unrealized P&amp;L</h2>")
    if not portfolio:
        parts.append("<p class='empty'>(none)</p>")
    else:
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


def write_report_file(report: str, label: str) -> Path:
    reports_dir = Path(settings.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"snapshot_{date.today().isoformat()}_{label}.html"
    path.write_text(report, encoding="utf-8")
    return path


def commit_report(path: Path, label: str) -> None:
    """Commit the report locally (no push) so the account history
    accumulates in version control without needing manual intervention."""
    try:
        subprocess.run(["git", "add", str(path)], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "commit", "-m", f"Snapshot: {label} {date.today().isoformat()}"],
            check=True, capture_output=True, text=True,
        )
        logger.info("Committed %s", path)
    except subprocess.CalledProcessError as e:
        # Most common cause: nothing changed (re-run same day), not a real failure.
        logger.warning("git commit skipped/failed for %s: %s", path, e.stderr.strip())


def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    if label not in ("premarket", "postmarket"):
        raise SystemExit("usage: python -m tradingbot.run_snapshot [premarket|postmarket]")

    with IBKRClient(settings, client_id=settings.snapshot_client_id) as client:
        report = build_report(client, label)

    path = write_report_file(report, label)
    print(f"Saved to {path}")
    commit_report(path, label)


if __name__ == "__main__":
    main()
