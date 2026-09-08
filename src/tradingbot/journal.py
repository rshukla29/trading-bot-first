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
