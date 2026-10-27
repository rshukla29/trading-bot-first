"""Realized P&L by strategy over a date range — for the daily, weekly and
monthly snapshot reports.

Sources:
  - ORB, ORB credit spreads, iron condor, Trend Join, Stock ORB: each bot's
    own journal table (complete trades — e.g. a credit spread's long leg
    that expired worthless is included). Their exit timestamps are UTC;
    they're converted to the ET trading date here. Option journals record
    P&L before commissions.
  - MA-crossover / RSI(2): IBKR's closed stock fills (after commissions),
    attributed to the bot whose BUY order came last before the sale (the
    signal it logged at the same moment names the bot). Ownership claims
    are deleted once a stock is sold, so they can't be used after the fact.
  - Anything else (manual trades like ASTX, unattributed fills) is "Manual /
    other".
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")

JOURNAL_STRATEGIES = (
    ("ORB (SPY 0DTE options)", "orb_positions", "exit_date"),
    ("ORB credit spreads", "credit_orb_positions", "exit_ts"),
    ("Iron condor", "condor_positions", "exit_date"),
    ("Trend Join", "trend_join_positions", "exit_ts"),
    ("Stock ORB", "stock_orb_positions", "exit_ts"),
)
STOCK_BOTS = {"run_ibkr_bot": "MA-crossover", "run_rsi2_bot": "RSI(2)"}


def _et_date(ts: str) -> date:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(ET).date()


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _stock_owner(conn, symbol: str, sold_on: date) -> str:
    """Which daily bot bought `symbol` most recently before it was sold."""
    end = datetime.combine(sold_on + timedelta(days=1), datetime.min.time(), ET).astimezone(ZoneInfo("UTC")).isoformat()
    buy = conn.execute("SELECT ts FROM orders WHERE symbol=? AND side='BUY' AND ts < ? ORDER BY ts DESC LIMIT 1",
                       (symbol, end)).fetchone()
    if buy is None:
        return "Manual / other"
    t = pd.Timestamp(buy[0])
    lo = (t - pd.Timedelta(minutes=5)).isoformat()
    sig = conn.execute(
        "SELECT source FROM signals WHERE symbol=? AND action='BUY' AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 1",
        (symbol, lo, buy[0])).fetchone()
    return STOCK_BOTS.get(sig[0], "Manual / other") if sig else "Manual / other"


def strategy_pnl(db_path: str, start: date, end: date) -> pd.DataFrame:
    """One row per strategy: trades, wins, realized P&L, for trades closed
    from `start` to `end` (ET dates, inclusive)."""
    rows: dict[str, list[float]] = {}

    def add(name: str, pnl: float) -> None:
        r = rows.setdefault(name, [0, 0, 0.0])
        r[0] += 1
        r[1] += 1 if pnl > 0 else 0
        r[2] += pnl

    conn = sqlite3.connect(db_path)
    try:
        journal_stock_days: set[tuple[str, date]] = set()
        for name, table, col in JOURNAL_STRATEGIES:
            rows.setdefault(name, [0, 0, 0.0])
            if not _table_exists(conn, table):
                continue
            for sym, ts, pnl in conn.execute(
                    f"SELECT symbol, {col}, realized_pnl FROM {table} WHERE status='closed' AND {col} IS NOT NULL"):
                d = _et_date(ts)
                if table in ("trend_join_positions", "stock_orb_positions"):
                    journal_stock_days.add((sym, d))
                if start <= d <= end:
                    add(name, pnl or 0.0)

        for label in STOCK_BOTS.values():
            rows.setdefault(label, [0, 0, 0.0])
        # One trade per stock per day (a stop can fill in several lots).
        sales: dict[tuple[str, date], float] = {}
        for sym, d, pnl in conn.execute(
                "SELECT symbol, date, realized_pnl FROM closed_trades WHERE date >= ? AND date <= ? "
                "AND symbol NOT LIKE '% %'", (start.isoformat(), end.isoformat())):
            day = date.fromisoformat(d)
            if (sym, day) in journal_stock_days:
                continue  # counted by Trend Join's / Stock ORB's own journal
            sales[(sym, day)] = sales.get((sym, day), 0.0) + pnl
        for (sym, day), pnl in sales.items():
            add(_stock_owner(conn, sym, day), pnl)
    finally:
        conn.close()

    df = pd.DataFrame([dict(strategy=k, trades=int(v[0]), wins=int(v[1]), realized=round(v[2], 2))
                       for k, v in rows.items()])
    order = [n for n, _, _ in JOURNAL_STRATEGIES] + list(STOCK_BOTS.values()) + ["Manual / other"]
    df["o"] = df["strategy"].map({n: i for i, n in enumerate(order)})
    return df.sort_values("o").drop(columns="o").reset_index(drop=True)
