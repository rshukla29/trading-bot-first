"""Start / Pause / Stop for each trading strategy. Set from the dashboard
(run_control_server.py), read by the bots every tick.

  run   - trade normally. The default: no control file, or a strategy
          that isn't in it.
  pause - no new entries. Open positions are still managed (stops,
          profit locks, targets, the 3:55 flatten).
  stop  - no new entries. The bot keeps managing what's open until it's
          closed, then exits; scheduled starts exit straight away. With
          close=True the bot first closes its own positions at market
          (never a manual position or another strategy's).
A mode lasts until it's changed: no daily reset.

The file (settings.control_path, data/control.json) is written atomically.
If it exists but can't be read, every strategy counts as PAUSED: a broken
control file must never let a paused bot trade.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tradingbot.config import settings

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

RUN, PAUSE, STOP = "run", "pause", "stop"
MODES = (RUN, PAUSE, STOP)
LABELS = {RUN: "Running", PAUSE: "Paused", STOP: "Stopped"}
LOG_KEEP = 50

# id -> display name, description, the Task Scheduler task that runs it,
# its module (a strategy whose module isn't in this copy of the repo is
# hidden), and whether it runs all day (True) or once a day.
STRATEGIES: dict[str, dict] = {
    "orb": dict(name="ORB", sub="SPY 0DTE options", task="ORBTradingBot",
                module="tradingbot.run_orb_bot", intraday=True),
    "orb_hp": dict(name="ORB-0DTE-HP", sub="SPY 0DTE options, 5-min EMA/SMA/VWAP/MACD", task="OrbHpTradingBot",
                   module="tradingbot.run_orb_hp_bot", intraday=True),
    "credit_orb": dict(name="Credit ORB", sub="SPY credit spreads", task="CreditOrbTradingBot",
                       module="tradingbot.run_credit_orb_bot", intraday=True),
    "iron_condor": dict(name="Iron condor", sub="SPY, 45 days out (3:45pm)", task="IronCondorTradingBot",
                        module="tradingbot.run_iron_condor_bot", intraday=False),
    "stock_orb": dict(name="Stock ORB", sub="Top-mover stocks", task="StockOrbTradingBot",
                      module="tradingbot.run_stock_orb_bot", intraday=True),
    "trend_join": dict(name="Trend Join", sub="Stocks, intraday", task="TrendJoinTradingBot",
                       module="tradingbot.run_trend_join_bot", intraday=True),
    "rsi2": dict(name="RSI(2)", sub="Stocks, swing (4:30pm)", task="RSI2TradingBot",
                 module="tradingbot.run_rsi2_bot", intraday=False),
    "ma_crossover": dict(name="MA crossover", sub="Stocks, daily (4:30pm)", task="RSI2TradingBot",
                         module="tradingbot.run_ibkr_bot", intraday=False),
}


class ControlFileError(Exception):
    pass


def available() -> dict[str, dict]:
    """The strategies whose bot exists in this copy of the repo."""
    return {k: v for k, v in STRATEGIES.items() if importlib.util.find_spec(v["module"]) is not None}


def _path(path: str | Path | None) -> Path:
    return Path(path or settings.control_path)


def read_all(path: str | Path | None = None) -> dict:
    """{"strategies": {id: {"mode", "close", "since", "by"}}, "log": [...]}.
    Raises ControlFileError if the file exists but isn't valid."""
    p = _path(path)
    if not p.exists():
        return {"strategies": {}, "log": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("strategies", {}), dict):
            raise ValueError("not a control file")
    except (OSError, ValueError) as exc:
        raise ControlFileError(f"{p}: {exc}") from exc
    data.setdefault("strategies", {})
    data.setdefault("log", [])
    return data


def _write(data: dict, path: str | Path | None) -> None:
    p = _path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    for attempt in range(10):  # a bot reading the file at that instant blocks the rename on Windows
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05)


def entry(sid: str, path: str | Path | None = None) -> dict:
    """This strategy's record; paused if the control file can't be read."""
    try:
        rec = read_all(path)["strategies"].get(sid) or {}
    except ControlFileError as exc:
        logger.error("Control file unreadable — treating %s as paused: %s", sid, exc)
        return {"mode": PAUSE, "close": False, "error": str(exc)}
    m = rec.get("mode", RUN)
    return {**rec, "mode": m if m in MODES else PAUSE, "close": bool(rec.get("close")) and m == STOP}


def mode(sid: str, path: str | Path | None = None) -> str:
    return entry(sid, path)["mode"]


def entries_allowed(sid: str, path: str | Path | None = None) -> bool:
    return mode(sid, path) == RUN


def wants_close(sid: str, path: str | Path | None = None) -> bool:
    return entry(sid, path)["close"]


def set_mode(sid: str, new_mode: str, close: bool = False, by: str = "dashboard",
             path: str | Path | None = None) -> dict:
    if sid not in STRATEGIES:
        raise ValueError(f"unknown strategy {sid!r}")
    if new_mode not in MODES:
        raise ValueError(f"unknown mode {new_mode!r}")
    try:
        data = read_all(path)
    except ControlFileError:
        logger.error("Control file unreadable — starting a new one")
        data = {"strategies": {}, "log": []}
    now = datetime.now(ET).isoformat(timespec="seconds")
    close = bool(close) and new_mode == STOP
    data["strategies"][sid] = {"mode": new_mode, "close": close, "since": now, "by": by}
    what = LABELS[new_mode] + (" (close positions at market)" if close else
                               " (keep managing open positions)" if new_mode == STOP else "")
    data["log"] = ([{"at": now, "strategy": sid, "mode": new_mode, "close": close, "by": by,
                     "text": f"{STRATEGIES[sid]['name']} → {what}"}] + data["log"])[:LOG_KEEP]
    _write(data, path)
    return data["strategies"][sid]


def close_done(sid: str, path: str | Path | None = None) -> None:
    """The bot closed its positions: clear the close request (stays stopped)."""
    try:
        data = read_all(path)
    except ControlFileError:
        return
    rec = data["strategies"].get(sid)
    if not rec or not rec.get("close"):
        return
    rec["close"] = False
    now = datetime.now(ET).isoformat(timespec="seconds")
    data["log"] = ([{"at": now, "strategy": sid, "mode": rec.get("mode", STOP), "close": False, "by": "bot",
                     "text": f"{STRATEGIES[sid]['name']}: positions closed at market"}] + data["log"])[:LOG_KEEP]
    _write(data, path)


class Gate:
    """A bot's view of its control mode, logging each change once."""

    def __init__(self, sid: str, log: logging.Logger | None = None, path: str | Path | None = None):
        self.sid, self.path, self.log = sid, path, log or logger
        self._last: str | None = None

    def mode(self) -> str:
        m = mode(self.sid, self.path)
        if m != self._last:
            if m == RUN:
                if self._last is not None:
                    self.log.info("Control: %s is running again — new entries allowed", self.sid)
            elif m == PAUSE:
                self.log.warning("Control: %s is PAUSED — no new entries, open positions still managed", self.sid)
            else:
                self.log.warning("Control: %s is STOPPED — no new entries%s", self.sid,
                                 ", closing positions at market" if wants_close(self.sid, self.path) else "")
            self._last = m
        return m

    def entries_allowed(self) -> bool:
        return self.mode() == RUN

    def stopped(self) -> bool:
        return self.mode() == STOP

    def wants_close(self) -> bool:
        return wants_close(self.sid, self.path)

    def close_done(self) -> None:
        close_done(self.sid, self.path)
