from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    price REAL NOT NULL,
    reason TEXT NOT NULL,
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    stop_price REAL,
    broker TEXT NOT NULL,
    broker_order_id TEXT,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    fill_price REAL NOT NULL,
    broker TEXT NOT NULL,
    broker_order_id TEXT,
    realized_pnl REAL
);

-- IBKR's position list is just symbol+quantity per option leg — it carries
-- no notion of "these 4 legs are one iron condor". This table is our own
-- bookkeeping so the live bot can find its own open positions again.
CREATE TABLE IF NOT EXISTS condor_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    expiry TEXT NOT NULL,
    call_short_strike REAL NOT NULL,
    call_long_strike REAL NOT NULL,
    put_short_strike REAL NOT NULL,
    put_long_strike REAL NOT NULL,
    call_short_conid INTEGER NOT NULL,
    call_long_conid INTEGER NOT NULL,
    put_short_conid INTEGER NOT NULL,
    put_long_conid INTEGER NOT NULL,
    credit_received REAL NOT NULL,
    contracts INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    exit_date TEXT,
    exit_cost REAL,
    exit_reason TEXT,
    realized_pnl REAL
);

-- run_orb_bot.py's single-leg long-option position. Only ever one open at
-- a time (mirrors the backtest's single open_trade slot for one symbol).
-- Persisted (not just kept in memory) so a crashed/restarted process can
-- recover which position it's still responsible for monitoring — a 0DTE
-- long option left unmonitored is a real risk.
CREATE TABLE IF NOT EXISTS orb_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    right TEXT NOT NULL,
    conid INTEGER NOT NULL,
    entry_premium REAL NOT NULL,
    contracts INTEGER NOT NULL,
    range_low REAL NOT NULL,
    range_high REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    exit_date TEXT,
    exit_premium REAL,
    exit_reason TEXT,
    realized_pnl REAL
);

-- run_trend_join_bot.py's intraday stock positions. Persisted for the same
-- reason as orb_positions (restart recovery), plus it records which
-- symbols this strategy owns — IBKR positions carry no strategy tag, and
-- run_rsi2_bot.py otherwise treats any non-watchlist stock as its own.
-- stop_order_id is the broker-side protective stop, modified in place as
-- the stop moves to breakeven and trails.
CREATE TABLE IF NOT EXISTS trend_join_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    entry_ts TEXT NOT NULL,
    entry_price REAL NOT NULL,
    qty INTEGER NOT NULL,
    remaining INTEGER NOT NULL,
    initial_stop REAL NOT NULL,
    stop_price REAL NOT NULL,
    stop_order_id INTEGER,
    partial_taken INTEGER NOT NULL DEFAULT 0,
    breakeven INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    exit_ts TEXT,
    exit_reason TEXT,
    realized_pnl REAL
);

-- Which daily stock bot (run_ibkr_bot = "ma_crossover", run_rsi2_bot =
-- "rsi2") opened each stock position. IBKR positions carry no strategy
-- tag, so without this a bot would manage (and could sell) any position on
-- its watchlist — including ones bought manually. A bot only manages
-- symbols it has claimed here; see ownership.py.
CREATE TABLE IF NOT EXISTS stock_ownership (
    symbol TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    ts TEXT NOT NULL
);

-- One row per calendar date, recorded by the postmarket snapshot after
-- that day's trading is done. Gives weekly/monthly reports a real
-- baseline balance to compare against — IBKR's own API has no
-- calendar-period PnL tag (see run_snapshot.py's YTD comment for the
-- same constraint), so period P&L is computed as (current
-- NetLiquidation - the balance recorded on the last trading day before
-- the period started).
CREATE TABLE IF NOT EXISTS daily_balances (
    date TEXT PRIMARY KEY,
    net_liquidation REAL NOT NULL,
    ts TEXT NOT NULL
);

-- Individual closed-position fills, recorded by every snapshot run (see
-- run_snapshot.py). IBKR's reqExecutions() only returns *today's*
-- executions regardless of the requested start time — confirmed live: a
-- filter asking for everything since 2026-09-01 still returned only
-- same-day fills — so a weekly/monthly report can't reconstruct earlier
-- closes by re-querying IBKR after the fact. This table is the only
-- record of them once the day they happened on has passed. exec_id is
-- IBKR's own unique execution id, giving natural, safe dedup across
-- reruns of the same day's snapshot.
CREATE TABLE IF NOT EXISTS closed_trades (
    exec_id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    realized_pnl REAL NOT NULL,
    ts TEXT NOT NULL
);
"""


class Journal:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def log_signal(self, symbol: str, action: str, price: float, reason: str, source: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO signals (ts, symbol, action, price, reason, source) VALUES (?, ?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), symbol, action, price, reason, source),
            )

    def log_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        broker: str,
        status: str,
        stop_price: float | None = None,
        broker_order_id: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO orders
                   (ts, symbol, side, quantity, order_type, stop_price, broker, broker_order_id, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    symbol,
                    side,
                    quantity,
                    order_type,
                    stop_price,
                    broker,
                    broker_order_id,
                    status,
                ),
            )

    def open_condor_position(
        self,
        symbol: str,
        entry_date: str,
        expiry: str,
        call_short_strike: float,
        call_long_strike: float,
        put_short_strike: float,
        put_long_strike: float,
        call_short_conid: int,
        call_long_conid: int,
        put_short_conid: int,
        put_long_conid: int,
        credit_received: float,
        contracts: int,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO condor_positions
                   (symbol, entry_date, expiry, call_short_strike, call_long_strike,
                    put_short_strike, put_long_strike, call_short_conid, call_long_conid,
                    put_short_conid, put_long_conid, credit_received, contracts, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
                (
                    symbol, entry_date, expiry, call_short_strike, call_long_strike,
                    put_short_strike, put_long_strike, call_short_conid, call_long_conid,
                    put_short_conid, put_long_conid, credit_received, contracts,
                ),
            )
            return cur.lastrowid

    def open_condor_positions(self, symbol: str | None = None) -> list[sqlite3.Row]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            if symbol is not None:
                rows = conn.execute(
                    "SELECT * FROM condor_positions WHERE status='open' AND symbol=?", (symbol,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM condor_positions WHERE status='open'").fetchall()
            return rows

    def last_condor_entry_date(self, symbol: str) -> str | None:
        """Most recent entry_date for `symbol`, open or closed — used to
        enforce entry cadence even across closed positions."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT entry_date FROM condor_positions WHERE symbol=? ORDER BY entry_date DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            return row[0] if row else None

    def close_condor_position(self, position_id: int, exit_date: str, exit_cost: float, exit_reason: str, realized_pnl: float) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE condor_positions
                   SET status='closed', exit_date=?, exit_cost=?, exit_reason=?, realized_pnl=?
                   WHERE id=?""",
                (exit_date, exit_cost, exit_reason, realized_pnl, position_id),
            )

    def open_orb_position(
        self,
        symbol: str,
        entry_date: str,
        expiry: str,
        strike: float,
        right: str,
        conid: int,
        entry_premium: float,
        contracts: int,
        range_low: float,
        range_high: float,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO orb_positions
                   (symbol, entry_date, expiry, strike, right, conid, entry_premium,
                    contracts, range_low, range_high, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
                (symbol, entry_date, expiry, strike, right, conid, entry_premium, contracts, range_low, range_high),
            )
            return cur.lastrowid

    def open_orb_position_row(self, symbol: str) -> sqlite3.Row | None:
        """At most one open ORB position at a time — used both to check
        "is anything open" and, on process restart, to recover which
        position needs continued exit monitoring."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM orb_positions WHERE status='open' AND symbol=? ORDER BY id DESC LIMIT 1", (symbol,)
            ).fetchone()

    def close_orb_position(self, position_id: int, exit_date: str, exit_premium: float, exit_reason: str, realized_pnl: float) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE orb_positions
                   SET status='closed', exit_date=?, exit_premium=?, exit_reason=?, realized_pnl=?
                   WHERE id=?""",
                (exit_date, exit_premium, exit_reason, realized_pnl, position_id),
            )

    def open_trend_join_position(
        self, symbol: str, trade_date: str, entry_price: float, qty: int,
        initial_stop: float, stop_order_id: int | None, reason: str,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO trend_join_positions
                   (symbol, trade_date, entry_ts, entry_price, qty, remaining, initial_stop,
                    stop_price, stop_order_id, reason, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
                (symbol, trade_date, datetime.now(timezone.utc).isoformat(), entry_price, qty, qty,
                 initial_stop, initial_stop, stop_order_id, reason),
            )
            return cur.lastrowid

    def open_trend_join_positions(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM trend_join_positions WHERE status='open' ORDER BY id").fetchall()

    def trend_join_symbols_traded_on(self, trade_date: str) -> list[str]:
        """Every symbol entered on `trade_date`, open or closed — enforces
        max trades/day and one entry per symbol per day."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol FROM trend_join_positions WHERE trade_date=? ORDER BY id", (trade_date,)
            ).fetchall()
            return [r[0] for r in rows]

    def update_trend_join_position(self, position_id: int, **fields) -> None:
        allowed = {"remaining", "stop_price", "stop_order_id", "partial_taken", "breakeven", "realized_pnl"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown trend_join_positions fields: {unknown}")
        assignments = ", ".join(f"{k}=?" for k in fields)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE trend_join_positions SET {assignments} WHERE id=?", (*fields.values(), position_id)
            )

    def close_trend_join_position(self, position_id: int, exit_reason: str, realized_pnl: float) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE trend_join_positions
                   SET status='closed', remaining=0, exit_ts=?, exit_reason=?, realized_pnl=?
                   WHERE id=?""",
                (datetime.now(timezone.utc).isoformat(), exit_reason, realized_pnl, position_id),
            )

    def claim_stock(self, symbol: str, strategy: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO stock_ownership (symbol, strategy, ts) VALUES (?, ?, ?)
                   ON CONFLICT(symbol) DO UPDATE SET strategy=excluded.strategy, ts=excluded.ts""",
                (symbol, strategy, datetime.now(timezone.utc).isoformat()),
            )

    def release_stock(self, symbol: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM stock_ownership WHERE symbol=?", (symbol,))

    def owned_stocks(self, strategy: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT symbol FROM stock_ownership WHERE strategy=?", (strategy,)).fetchall()
            return {r[0] for r in rows}

    def log_fill(
        self,
        symbol: str,
        side: str,
        quantity: int,
        fill_price: float,
        broker: str,
        broker_order_id: str | None = None,
        realized_pnl: float | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO fills
                   (ts, symbol, side, quantity, fill_price, broker, broker_order_id, realized_pnl)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    symbol,
                    side,
                    quantity,
                    fill_price,
                    broker,
                    broker_order_id,
                    realized_pnl,
                ),
            )

    def record_daily_balance(self, date: str, net_liquidation: float) -> None:
        """Upsert — safe to call more than once for the same date (e.g. a
        rerun), always keeping the latest value recorded for that date."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO daily_balances (date, net_liquidation, ts) VALUES (?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET net_liquidation=excluded.net_liquidation, ts=excluded.ts""",
                (date, net_liquidation, datetime.now(timezone.utc).isoformat()),
            )

    def balance_before(self, date: str) -> float | None:
        """Most recent recorded balance strictly before `date` (YYYY-MM-DD)
        — the baseline for a period starting on `date`. None if no balance
        has ever been recorded before then (e.g. the very first week this
        table existed)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT net_liquidation FROM daily_balances WHERE date < ? ORDER BY date DESC LIMIT 1",
                (date,),
            ).fetchone()
            return row[0] if row else None

    def record_closed_trade(
        self, exec_id: str, date: str, symbol: str, side: str, quantity: float, price: float, realized_pnl: float,
    ) -> None:
        """INSERT OR IGNORE: exec_id is IBKR's own unique execution id, so
        re-recording the same fill on a later rerun of the same day's
        snapshot is a safe no-op, not a duplicate."""
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO closed_trades
                   (exec_id, date, symbol, side, quantity, price, realized_pnl, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (exec_id, date, symbol, side, quantity, price, realized_pnl, datetime.now(timezone.utc).isoformat()),
            )

    def closed_trades_between(self, start_date: str, end_date: str) -> list[sqlite3.Row]:
        """Inclusive of both endpoints (YYYY-MM-DD)."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM closed_trades WHERE date >= ? AND date <= ? ORDER BY ts",
                (start_date, end_date),
            ).fetchall()
