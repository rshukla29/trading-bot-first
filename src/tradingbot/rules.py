"""Trading rules set from the dashboard's Rules tab: the capital the bots
trade with, and each strategy's own size, caps, stops and daily loss limit.

Saved to data/rules.json (settings.rules_path, not in git, so updates never
touch it). The file only holds what the user changed: everything else is
today's setting from .env / config.py, so an empty or missing file changes
nothing. Every save is appended to data/rules_log.jsonl.

Bots call `load("orb")` (cheap: the file is re-read only when it changes)
and use the accessors on the returned StrategyRules. A broken file, or a
value that fails its check, is logged as an error and that value falls back
to its default: a bot never trades on a bad rule.

Units, as on the dashboard: percentages are written as percent (1 = 1%),
option prices per share (x100 per contract), $ amounts in dollars.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tradingbot.config import Settings, settings as _settings

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
EPS = 1e-9
MAX_RISK_PCT = 5.0       # of capital, per trade
MAX_CONTRACTS = 50
MAX_DAILY_PCT = 20.0
LOG_KEEP = 200

# Strategy id (control.py) -> name in strategy_pnl.py's realized-P&L table.
PNL_NAMES = {
    "orb": "ORB (SPY 0DTE options)", "orb_hp": "ORB-0DTE-HP (SPY 0DTE options)",
    "credit_orb": "ORB credit spreads", "iron_condor": "Iron condor",
    "trend_join": "Trend Join", "stock_orb": "Stock ORB", "rsi2": "RSI(2)", "ma_crossover": "MA-crossover",
}


# --------------------------------------------------------------------------- schema
def _size(pct_label: str, usd_label: str, unit: str, first: str = "pct") -> dict:
    modes = {"pct": dict(label=pct_label, suffix="%"), "usd": dict(label=usd_label, prefix="$"),
             "fixed": dict(label="Fixed", suffix=unit)}
    order = [first] + [m for m in ("pct", "usd", "fixed") if m != first]
    return dict(t="modes", key="size", label="Position size", modes=[dict(k=k, **modes[k]) for k in order])


def _daily() -> dict:
    return dict(t="modes", key="daily", label="Daily loss limit",
                modes=[dict(k="off", label="Off"), dict(k="usd", label="$", prefix="$"),
                       dict(k="pct", label="% of capital", suffix="%")],
                hint="No new trades for the rest of the day once today's realized losses reach this.")


NEW_ONLY = "Applies from the next new trade. Open trades keep the rules they started with."
LADDER_HINT = ("Starts once the trade is up by the first amount, then rises with every further step. "
               "It never moves down.")

# Field definitions, shared by the dashboard (sent as JSON) and the checks below.
SCHEMA: dict[str, dict] = {
    "orb": dict(name="ORB", desc="SPY same-day options, bought on the opening-range breakout", unit="contracts",
                opt=True, open_note="Stop loss and step-stop changes also reach open positions within a minute. Size changes apply to new trades only.", example=[dict(key="p", label="Option price $", val=1.40, step=0.05)],
                fields=[
        dict(_size("% of capital", "$ per trade", "contracts"),
             hint="% and $ set how much premium one trade may spend."),
        dict(t="num", key="maxQty", label="Max contracts", suffix="contracts", int=True, min=1, max=MAX_CONTRACTS,
             hint="Hard cap whatever the size gives."),
        dict(t="modes", key="maxPrem", label="Max premium",
             modes=[dict(k="off", label="Off"), dict(k="usd", label="$ option price", prefix="$"),
                    dict(k="pct", label="% of capital", suffix="%")],
             hint="Skip the trade when one contract costs more than this. $ is the option price (x100 per contract)."),
        dict(t="modes", key="stop", label="Stop loss",
             modes=[dict(k="off", label="Off"), dict(k="pct", label="% of premium", suffix="%"),
                    dict(k="usd", label="$ per contract", prefix="$")],
             hint="Sell when the option falls this much from what you paid."),
        dict(t="ladder", key="ladder", label="First & step stop", per="per contract"),
        dict(t="modes", key="target", label="Profit target",
             modes=[dict(k="off", label="Off"), dict(k="pct", label="% gain", suffix="%")], hint="Sell all at this gain."),
        dict(t="num", key="trades", label="Max trades per day", suffix="per day", int=True, min=1, max=20,
             hint="Calls and puts together."),
        _daily(),
    ]),
    "credit_orb": dict(name="Credit ORB", desc="SPY same-day credit spreads after the first hour's range",
                       unit="contracts", opt=True, open_note=NEW_ONLY,
                       example=[dict(key="c", label="Credit $", val=0.40, step=0.05)], fields=[
        dict(_size("% of capital", "$ max loss per trade", "contracts", first="usd"),
             hint="Contracts = this ÷ ((width − credit) x 100)."),
        dict(t="num", key="maxQty", label="Max contracts", suffix="contracts", int=True, min=1, max=MAX_CONTRACTS),
        dict(t="num", key="width", label="Spread width", prefix="$", step=0.5, min=0.5, max=50,
             hint="Distance between the two strikes."),
        dict(t="num", key="minCredit", label="Min credit", prefix="$", step=0.05, min=0.01, max=10,
             hint="Skip the trade when the spread pays less than this (x100 per contract)."),
        dict(t="info", label="Stop loss",
             text="Built in: the most a spread can lose is the width minus the credit. Closed by 3:50pm."),
        _daily(),
    ]),
    "iron_condor": dict(name="Iron condor", desc="SPY 45-day iron condors, opened weekly", unit="contracts",
                        opt=True, open_note="Profit target and stop changes also reach open condors at the next 3:45pm check.",
                        example=[dict(key="c", label="Credit $", val=1.50, step=0.05)],
                        fields=[
        dict(_size("% of capital at risk", "$ at risk", "contracts"),
             hint="At risk = (wing − credit) x 100 per contract."),
        dict(t="num", key="maxQty", label="Max contracts", suffix="contracts", int=True, min=1, max=MAX_CONTRACTS),
        dict(t="num", key="maxOpen", label="Max open at once", suffix="condors", int=True, min=1, max=20),
        dict(t="num", key="wing", label="Wing width", prefix="$", step=1, min=1, max=50),
        dict(t="modes", key="target", label="Profit target",
             modes=[dict(k="off", label="Off"), dict(k="pct", label="% of credit", suffix="%")],
             hint="Close when this much of the credit is kept."),
        dict(t="num", key="stopX", label="Stop loss", suffix="x credit", step=0.5, min=1.1, max=10,
             hint="Close when buying it back costs this many times the credit."),
    ]),
    "stock_orb": dict(name="Stock ORB", desc="Top-mover stocks breaking their 15-minute opening range", unit="shares",
                      open_note=NEW_ONLY,
                      example=[dict(key="e", label="Entry $", val=50, step=1), dict(key="s", label="Stop $", val=49, step=0.5)],
                      fields=[
        dict(_size("% of capital at risk", "$ at risk", "shares", first="usd"), hint="At risk = shares x (entry − stop)."),
        dict(t="num", key="maxVal", label="Max position", prefix="$", step=500, min=100, max=1e7),
        dict(t="num", key="trades", label="Trades per day", suffix="per day", int=True, min=1, max=50),
        dict(t="info", label="Stop loss", text="The middle of the opening range (set by the strategy)."),
        dict(t="num", key="targetR", label="Profit target", suffix="x risk", step=0.5, min=0.25, max=20),
        dict(_daily(), hint="Counts the risk of today's trades (entry − stop), so a trade that could break the "
                            "limit isn't placed."),
    ]),
    "trend_join": dict(name="Trend join", desc="Gappers joining a strong trend, intraday", unit="shares",
                       open_note="Profit-taking and breakeven changes also reach open positions. Size changes apply to new trades only.",
                       example=[dict(key="e", label="Entry $", val=20, step=1), dict(key="s", label="Day's low $", val=19.4, step=0.1)],
                       fields=[
        _size("% of capital at risk", "$ at risk", "shares"),
        dict(t="num", key="maxVal", label="Max position", prefix="$", step=500, min=100, max=1e7),
        dict(t="num", key="maxOpen", label="Max open at once", suffix="positions", int=True, min=1, max=50),
        dict(t="num", key="trades", label="Trades per day", suffix="per day", int=True, min=1, max=50),
        dict(t="info", label="Stop loss", text="1% below the day's low (set by the strategy)."),
        dict(t="num", key="partialR", label="Take ⅓ profit at", suffix="x risk", step=0.25, min=0.25, max=20),
        dict(t="num", key="beR", label="Stop to breakeven at", suffix="x risk", step=0.25, min=0.25, max=20),
        _daily(),
    ]),
    "rsi2": dict(name="RSI(2)", desc="Daily dip-buys on large caps, held a few days", unit="shares",
                 open_note="Stop loss and step-stop changes also reach open positions within a minute. Size changes apply to new trades only.", example=[dict(key="e", label="Entry $", val=60, step=1)], fields=[
        dict(_size("% of capital at risk", "$ at risk", "shares"), hint="At risk = shares x the stop distance."),
        dict(t="num", key="maxVal", label="Max position", prefix="$", step=500, min=100, max=1e7),
        dict(t="num", key="maxOpen", label="Max open at once", suffix="positions", int=True, min=1, max=50),
        dict(t="modes", key="stop", label="Stop loss",
             modes=[dict(k="pct", label="% below entry", suffix="%"), dict(k="usd", label="$ per share", prefix="$")]),
        dict(t="ladder", key="ladder", label="First & step stop", per="per share"),
        _daily(),
    ]),
}
# ORB-0DTE-HP: same option fields as ORB (calls and puts, several trades a day).
SCHEMA["orb_hp"] = dict(
    SCHEMA["orb"], name="ORB-0DTE-HP",
    desc="SPY same-day options when 5-minute candles line up with the 9 EMA, 50/200 SMA, VWAP and MACD",
    fields=[dict(f, hint="Calls and puts together; each entry is one trade.") if f.get("key") == "trades" else dict(f)
            for f in SCHEMA["orb"]["fields"]])
SCHEMA = {k: SCHEMA[k] for k in ["orb", "orb_hp", *[k for k in SCHEMA if k not in ("orb", "orb_hp")]]}
SCHEMA["ma_crossover"] = dict(SCHEMA["rsi2"], name="MA crossover", desc="Daily trend-following on the watchlist",
                              example=[dict(key="e", label="Entry $", val=150, step=1)])
for _f in SCHEMA["rsi2"]["fields"] + SCHEMA["orb"]["fields"] + SCHEMA["orb_hp"]["fields"]:
    if _f["t"] == "ladder":
        _f["hint"] = LADDER_HINT
STRATEGIES = list(SCHEMA)


def _lock_from_trail(trig: float, step: float, trail: float) -> float:
    """config.py's ladder (trigger, step, trail) as the dashboard's 'once up
    trig, lock in lock': the stop when the trigger is first reached."""
    if trig <= 0 or step <= 0:
        return 0.0
    return round((math.floor(trig / step + EPS) * step - trail) * 100, 4)


def defaults(s: Settings | None = None) -> dict[str, dict]:
    """Today's rules, from .env / config.py. The 'other mode' values (e.g. the
    $ amount while sizing by %) are only starting points for the dashboard."""
    s = s or _settings
    p = lambda x: round(x * 100, 4)  # noqa: E731
    stock_ladder = dict(on=bool(s.step_stop_enabled and s.stock_lock_trigger_pct > 0), mode="pct",
                        trig=p(s.stock_lock_trigger_pct), step=p(s.stock_lock_step_pct),
                        lock=_lock_from_trail(s.stock_lock_trigger_pct, s.stock_lock_step_pct, s.stock_lock_trail_pct))
    # Off until set on the Rules tab: the only limit RSI(2) / MA crossover use is the
    # one the user saves for that strategy ($ or % of capital), never a built-in %.
    daily_stock = dict(mode="off", usd=500.0, pct=2.0)
    return {
        "orb": dict(
            size=dict(mode="pct", pct=p(s.options_risk_pct_per_trade), usd=250.0, fixed=1),
            maxQty=s.options_max_contracts_per_trade, maxPrem=dict(mode="off", usd=3.0, pct=1.0),
            stop=dict(mode="pct" if s.options_use_stop_loss else "off", pct=p(s.options_stop_loss_pct_of_premium), usd=40.0),
            ladder=dict(on=s.options_ladder_trigger_pct > 0, mode="pct", trig=p(s.options_ladder_trigger_pct) or 50.0,
                        step=p(s.options_ladder_step_pct),
                        lock=_lock_from_trail(s.options_ladder_trigger_pct, s.options_ladder_step_pct,
                                              s.options_ladder_trail_pct) if s.options_ladder_trigger_pct > 0 else 25.0),
            target=dict(mode="pct" if s.options_profit_target_pct else "off", pct=p(s.options_profit_target_pct) or 50.0),
            # One breakout per direction plus re-entries, calls and puts together.
            trades=2 * (1 + max(0, s.options_max_reentries)),
            daily=dict(mode="off", usd=500.0, pct=2.0)),
        # The rules exit on the candle colour: no stop, profit lock or target until set.
        "orb_hp": dict(
            size=dict(mode="pct", pct=p(s.orb_hp_risk_pct_per_trade), usd=250.0, fixed=1),
            maxQty=s.orb_hp_max_contracts_per_trade, maxPrem=dict(mode="off", usd=3.0, pct=1.0),
            stop=dict(mode="off", pct=30.0, usd=40.0),
            ladder=dict(on=False, mode="pct", trig=50.0, step=25.0, lock=25.0),
            target=dict(mode="off", pct=50.0),
            trades=s.orb_hp_max_trades_per_day,
            daily=dict(mode="off", usd=500.0, pct=2.0)),
        "credit_orb": dict(
            size=dict(mode="usd", pct=2.0, usd=float(s.credit_orb_max_loss_dollars), fixed=1),
            maxQty=s.credit_orb_max_contracts, width=float(s.credit_orb_width), minCredit=float(s.credit_orb_min_credit),
            daily=dict(mode="off", usd=500.0, pct=2.0)),
        "iron_condor": dict(
            size=dict(mode="pct", pct=p(s.credit_spread_risk_pct_per_trade), usd=500.0, fixed=1),
            maxQty=s.credit_spread_max_contracts_per_trade, maxOpen=s.credit_spread_max_concurrent,
            wing=float(s.credit_spread_wing_width),
            target=dict(mode="pct" if s.credit_spread_profit_target_pct else "off",
                        pct=p(s.credit_spread_profit_target_pct) or 50.0),
            stopX=float(s.credit_spread_stop_loss_multiple)),
        "stock_orb": dict(
            size=dict(mode="usd", pct=0.2, usd=float(s.stock_orb_max_loss_per_trade), fixed=10),
            maxVal=float(s.stock_orb_max_order_value), trades=s.stock_orb_max_trades_per_day,
            targetR=float(s.stock_orb_target_r),
            daily=dict(mode="usd" if s.stock_orb_max_daily_loss > 0 else "off", usd=float(s.stock_orb_max_daily_loss) or 150.0,
                       pct=0.6)),
        "trend_join": dict(
            size=dict(mode="pct", pct=p(s.risk_pct_per_trade), usd=250.0, fixed=10),
            maxVal=float(s.max_position_value), maxOpen=s.trend_join_max_positions, trades=s.trend_join_max_trades_per_day,
            partialR=float(s.trend_join_partial_r), beR=float(s.trend_join_breakeven_r),
            daily=dict(mode="off", usd=500.0, pct=2.0)),
        "rsi2": dict(
            size=dict(mode="pct", pct=p(s.risk_pct_per_trade), usd=250.0, fixed=10),
            maxVal=float(s.max_position_value), maxOpen=s.max_positions,
            stop=dict(mode="pct", pct=p(s.rsi2_stop_loss_pct), usd=5.0),
            ladder=copy.deepcopy(stock_ladder), daily=copy.deepcopy(daily_stock)),
        "ma_crossover": dict(
            size=dict(mode="pct", pct=p(s.risk_pct_per_trade), usd=250.0, fixed=10),
            maxVal=float(s.max_position_value), maxOpen=s.max_positions,
            stop=dict(mode="pct", pct=p(s.stop_loss_pct), usd=5.0),
            ladder=copy.deepcopy(stock_ladder), daily=copy.deepcopy(daily_stock)),
    }


# --------------------------------------------------------------------------- checks
def _num(x) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return float(x)


def check_field(sid: str, field: dict, value, capital: float) -> str | None:
    """The problem with one field's value, in words, or None if it's fine."""
    t, key = field["t"], field.get("key")
    if t == "num":
        v = _num(value)
        if v is None:
            return "Enter a number."
        if field.get("int") and v != int(v):
            return "Use a whole number."
        lo, hi = field.get("min", 0), field.get("max", 1e9)
        if not lo <= v <= hi:
            return f"Use a number from {lo:g} to {hi:g}."
        return None
    if t == "modes":
        if not isinstance(value, dict) or value.get("mode") not in [m["k"] for m in field["modes"]]:
            return "Choose one of the options."
        mode = value["mode"]
        if mode == "off":
            return None
        v = _num(value.get(mode))
        if v is None:
            return "Enter a number."
        if key == "size":
            if mode == "pct" and not 0 < v <= MAX_RISK_PCT:
                return f"Use more than 0% and at most {MAX_RISK_PCT:g}% of capital per trade."
            if mode == "usd" and not 0 < v <= capital * MAX_RISK_PCT / 100 + EPS:
                return f"Use more than $0 and at most ${capital * MAX_RISK_PCT / 100:,.0f} ({MAX_RISK_PCT:g}% of capital) per trade."
            if mode == "fixed":
                cap = MAX_CONTRACTS if SCHEMA[sid].get("opt") else 1_000_000
                if v != int(v) or not 1 <= v <= cap:
                    return f"Use a whole number from 1 to {cap:,}."
            return None
        if key == "stop" and mode == "pct" and not 0 < v < 100:
            return "Use more than 0% and less than 100%."
        if key == "target" and mode == "pct" and sid == "iron_condor" and not 0 < v < 100:
            return "Use more than 0% and less than 100%."
        if key == "daily" and mode == "pct" and not 0 < v <= MAX_DAILY_PCT:
            return f"Use more than 0% and at most {MAX_DAILY_PCT:g}% of capital."
        if key == "maxPrem" and mode == "pct" and not 0 < v <= MAX_RISK_PCT:
            return f"Use more than 0% and at most {MAX_RISK_PCT:g}% of capital."
        if v <= 0:
            return "Use more than 0, or turn it off."
        return None
    if t == "ladder":
        if not isinstance(value, dict) or not isinstance(value.get("on"), bool) or value.get("mode") not in ("pct", "usd"):
            return "Choose one of the options."
        if not value["on"]:
            return None
        trig, lock, step = (_num(value.get(k)) for k in ("trig", "lock", "step"))
        if None in (trig, lock, step):
            return "Enter a number in each box."
        if trig <= 0 or step <= 0:
            return "The first amount and the step must be more than 0."
        if lock < 0:
            return "The amount locked in can't be below 0."
        if lock >= trig:
            return "The stop must lock in less than the first amount, or it would sell straight away."
        return None
    return None


def check_strategy(sid: str, values: dict, capital: float) -> dict[str, str]:
    errs = {}
    for f in SCHEMA[sid]["fields"]:
        if f["t"] == "info":
            continue
        msg = check_field(sid, f, values.get(f["key"]), capital)
        if msg:
            errs[f["key"]] = msg
    v = values
    if sid == "credit_orb" and not errs.get("minCredit") and not errs.get("width") and v["minCredit"] >= v["width"]:
        errs["minCredit"] = "The min credit must be smaller than the spread width."
    return errs


# --------------------------------------------------------------------------- file
def path() -> Path:
    return Path(_settings.rules_path)


def log_path() -> Path:
    return path().with_name("rules_log.jsonl")


def read_file() -> dict:
    """The saved file as-is ({} when absent). Raises ValueError when broken."""
    p = path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{p} can't be read: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{p} isn't a rules file")
    return data


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = {**out[k], **{kk: vv for kk, vv in v.items() if kk in out[k]}}
        elif k in out:
            out[k] = v
    return out


def _diff(base: dict, cur: dict) -> dict:
    """The fields of `cur` that differ from `base` (whole field values)."""
    return {k: v for k, v in cur.items() if k in base and v != base[k]}


def effective(data: dict | None = None, s: Settings | None = None) -> tuple[float | None, dict[str, dict], list[str]]:
    """(capital or None, every strategy's rules, problems). A saved value
    that fails its check is replaced by its default and listed in problems."""
    s = s or _settings
    problems = []
    if data is None:
        try:
            data = read_file()
        except ValueError as exc:
            problems.append(f"{exc}. Using the default rules.")
            data = {}
    cap = _num(data.get("capital"))
    if data.get("capital") is not None and (cap is None or cap <= 0):
        problems.append("Saved capital isn't a positive amount. Using the whole account.")
        cap = None
    base = defaults(s)
    out = {}
    saved = data.get("strategies") if isinstance(data.get("strategies"), dict) else {}
    for sid in STRATEGIES:
        vals = _merge(base[sid], saved.get(sid) or {})
        for key, msg in check_strategy(sid, vals, cap or 1e12).items():
            problems.append(f"{SCHEMA[sid]['name']} {key}: {msg} Using the default.")
            vals[key] = copy.deepcopy(base[sid][key])
        out[sid] = vals
    return cap, out, problems


def read_log(limit: int = 50) -> list[dict]:
    p = log_path()
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines()[-LOG_KEEP:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    rows.sort(key=lambda r: r.get("at", ""), reverse=True)  # newest save first; a save's lines in order
    return rows[:limit]


def format_value(sid: str, field: dict, v) -> str:
    unit = SCHEMA[sid]["unit"]
    if field["t"] == "num":
        suffix = field.get("suffix", "")
        if v == 1 and suffix.endswith("s") and suffix[:-1].isalpha():
            suffix = suffix[:-1]
        num = f"{v:,.2f}" if field.get("prefix") == "$" and v % 1 else f"{v:,g}"
        return f"{field.get('prefix', '')}{num}{(' ' + suffix) if suffix else ''}"
    if field["t"] == "modes":
        m = v["mode"]
        usd = v.get("usd", 0)
        return {"off": "Off", "pct": f"{v.get('pct', 0):g}%", "usd": f"${usd:,.2f}" if usd % 1 else f"${usd:,.0f}",
                "fixed": f"{v.get('fixed', 0):g} {unit}"}[m]
    if field["t"] == "ladder":
        if not v["on"]:
            return "Off"
        u = "%" if v["mode"] == "pct" else "$"
        return (f"once up {v['trig']:g}{u} lock in {v['lock']:g}{u}, then steps of {v['step']:g}{u}"
                if u == "%" else f"once up ${v['trig']:g} lock in ${v['lock']:g}, then steps of ${v['step']:g}")
    return str(v)


def describe_changes(old_cap, old: dict, new_cap, new: dict) -> list[str]:
    lines = []
    if old_cap != new_cap:
        f = lambda c: f"${c:,.0f}" if c else "whole account"  # noqa: E731
        lines.append(f"Capital: {f(old_cap)} → {f(new_cap)}")
    for sid in STRATEGIES:
        for fld in SCHEMA[sid]["fields"]:
            k = fld.get("key")
            if k and old[sid].get(k) != new[sid].get(k):
                lines.append(f"{SCHEMA[sid]['name']} {fld['label'].lower()}: "
                             f"{format_value(sid, fld, old[sid][k])} → {format_value(sid, fld, new[sid][k])}")
    return lines


def save(body: dict, account_value: float | None = None) -> tuple[dict, list[str]]:
    """Check and save the dashboard's rules: body = {"capital": number|null,
    "strategies": {sid: {field: value}}}. Raises ValueError with every
    problem (field -> message) as JSON when anything is wrong."""
    s = _settings
    raw_cap = body.get("capital")
    cap = None if raw_cap in (None, "") else _num(raw_cap)
    errors: dict[str, dict] = {}
    if raw_cap not in (None, "") and (cap is None or cap <= 0):
        errors["_capital"] = {"capital": "Enter an amount above $0, or leave it blank for the whole account."}
    capital_for_checks = min(cap, account_value) if cap and account_value else (cap or account_value or 1e12)
    base = defaults(s)
    new = {}
    incoming = body.get("strategies") if isinstance(body.get("strategies"), dict) else {}
    for sid in STRATEGIES:
        vals = _merge(base[sid], incoming.get(sid) or {})
        errs = check_strategy(sid, vals, capital_for_checks)
        if errs:
            errors[sid] = errs
        new[sid] = vals
    if errors:
        raise ValueError(json.dumps({"problems": errors}))
    old_cap, old, _ = effective()
    data = {"capital": cap, "strategies": {sid: d for sid in STRATEGIES if (d := _diff(base[sid], new[sid]))},
            "saved_at": datetime.now(ET).isoformat(timespec="seconds")}
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    lines = describe_changes(old_cap, old, cap, new)
    if lines:
        stamp = datetime.now(ET).isoformat(timespec="microseconds")  # orders saves made in the same second
        with log_path().open("a", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps({"at": stamp, "text": line}) + "\n")
    _cache.clear()
    return data, lines


# --------------------------------------------------------------------------- dashboard
def account_value(db_path: str | None = None) -> float | None:
    """The latest account value the snapshots recorded (no IBKR call)."""
    try:
        conn = sqlite3.connect(db_path or _settings.db_path)
        try:
            row = conn.execute("SELECT net_liquidation FROM daily_balances ORDER BY date DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        return float(row[0]) if row else None
    except sqlite3.Error:
        return None


def payload() -> dict:
    """Everything the Rules tab shows."""
    try:
        raw = read_file()
        file_problem = None
    except ValueError as exc:
        raw, file_problem = {}, str(exc)
    cap, vals, problems = effective(raw)
    return {"schema": SCHEMA, "strategies": STRATEGIES, "defaults": defaults(), "values": vals,
            "capital": cap, "account_value": account_value(), "saved_at": raw.get("saved_at"),
            "problems": ([file_problem] if file_problem else []) + problems, "log": read_log(),
            "limits": {"max_risk_pct": MAX_RISK_PCT, "max_contracts": MAX_CONTRACTS, "max_daily_pct": MAX_DAILY_PCT}}


# --------------------------------------------------------------------------- bots
@dataclass
class StrategyRules:
    sid: str
    values: dict
    capital_setting: float | None

    def __getitem__(self, key):
        return self.values[key]

    def capital(self, account_value: float) -> float:
        """What % rules are a % of: the typed capital, never more than the account."""
        if self.capital_setting:
            return min(self.capital_setting, account_value) if account_value > 0 else self.capital_setting
        return account_value

    def _amount(self, key: str, capital: float) -> float | None:
        m = self.values.get(key)
        if not m or m["mode"] == "off":
            return None
        return capital * m["pct"] / 100 if m["mode"] == "pct" else float(m[m["mode"]])

    def budget(self, capital: float) -> float | None:
        """$ a trade may risk (or spend, for ORB); None when sizing is fixed."""
        return None if self.values["size"]["mode"] == "fixed" else self._amount("size", capital)

    def quantity(self, capital: float, risk_per_unit: float, cap: int | None = None) -> int:
        """Contracts/shares: fixed, or budget ÷ risk per unit; then capped."""
        if self.values["size"]["mode"] == "fixed":
            qty = int(self.values["size"]["fixed"])
        elif risk_per_unit <= 0:
            return 0
        else:
            qty = int(math.floor(self.budget(capital) / risk_per_unit + EPS))
        if cap is not None:
            qty = min(qty, int(cap))
        return max(0, qty)

    def stock_quantity(self, capital: float, price: float) -> int:
        """Shares for a stock entry at `price`: the size rule over the stop
        distance, capped by the max position value."""
        frac = self.stop_fraction(price)
        if price <= 0 or not frac:
            return 0
        return self.quantity(capital, price * frac, cap=int(self["maxVal"] // price))

    def stock_stop_pcts(self, positions: dict) -> dict[str, float]:
        """Each position's stop as a fraction of its cost ({symbol: Position})."""
        return {s: self.stop_fraction(p.avgCost) for s, p in positions.items() if p is not None}

    def daily_limit(self, capital: float) -> float | None:
        return self._amount("daily", capital)

    def max_premium(self, capital: float) -> float | None:
        """Most one option contract may cost, in $ (x100 already applied)."""
        m = self.values.get("maxPrem")
        if not m or m["mode"] == "off":
            return None
        return m["usd"] * 100 if m["mode"] == "usd" else capital * m["pct"] / 100

    def stop_fraction(self, entry: float, multiplier: float = 1) -> float | None:
        """The stop loss as a fraction of the entry price (None = off).
        $ stops are per contract for options (multiplier 100), per share for stocks."""
        m = self.values.get("stop")
        if not m or m["mode"] == "off" or entry <= 0:
            return None
        if m["mode"] == "pct":
            return m["pct"] / 100
        return min(m["usd"] / (entry * multiplier), 0.99)

    def profit_target(self) -> float | None:
        m = self.values.get("target")
        return None if not m or m["mode"] == "off" else m["pct"] / 100

    def ladder_stop(self, entry: float, peak: float, multiplier: float = 1) -> float | None:
        """Step-up stop price: once the best price is up by the first amount,
        the stop locks in `lock`; every further `step`, it rises by `step`.
        None before then (or when off). With the defaults this equals
        risk.ladder_stop (ORB 50/25/25, stocks 30/15/15)."""
        L = self.values.get("ladder")
        if not L or not L["on"] or entry <= 0:
            return None
        if L["mode"] == "pct":
            gain, trig, lock, step = peak / entry - 1, L["trig"] / 100, L["lock"] / 100, L["step"] / 100
            scale = entry
        else:
            gain, trig, lock, step = (peak - entry) * multiplier, L["trig"], L["lock"], L["step"]
            scale = 1 / multiplier
        if gain + EPS < trig:
            return None
        steps = math.floor((gain - trig) / step + EPS)
        return round(entry + (lock + steps * step) * scale, 2)


_cache: dict = {}


def load(sid: str) -> StrategyRules:
    """This strategy's rules now. The file is re-read only when it changes;
    defaults always come from the current settings. Problems are logged
    once per change of the file."""
    p = path()
    try:
        key = p.stat().st_mtime_ns if p.exists() else None
    except OSError:
        key = None
    if _cache.get("key") != key or "data" not in _cache:
        try:
            data, problem = read_file(), None
        except ValueError as exc:
            data, problem = {}, f"{exc}. Using the default rules."
        _cache.update(key=key, data=data, logged=False, problem=problem)
    cap, vals, problems = effective(_cache["data"])
    if not _cache["logged"]:
        for msg in ([_cache["problem"]] if _cache["problem"] else []) + problems:
            logger.error("Rules: %s", msg)
        _cache["logged"] = True
    return StrategyRules(sid, vals[sid], cap)


def realized_today(sid: str, day: date | None = None, db_path: str | None = None) -> float:
    """This strategy's realized P&L for today (ET) from the journal; 0 on error."""
    from tradingbot.strategy_pnl import strategy_pnl
    day = day or datetime.now(ET).date()
    try:
        df = strategy_pnl(db_path or _settings.db_path, day, day)
        row = df[df["strategy"] == PNL_NAMES[sid]]
        return float(row["realized"].iloc[0]) if not row.empty else 0.0
    except Exception:
        logger.exception("Rules: couldn't read today's realized P&L for %s", sid)
        return 0.0


def daily_limit_hit(r: StrategyRules, capital: float, db_path: str | None = None) -> float | None:
    """The limit, when today's realized losses have reached it; else None."""
    lim = r.daily_limit(capital)
    if lim is None:
        return None
    return lim if realized_today(r.sid, db_path=db_path) <= -lim else None
