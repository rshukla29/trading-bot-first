"""After-close daily facts for review: everything that happened today, per
strategy, in one markdown file that the headless Claude review (see
scripts/run_daily_review_scheduled.ps1) reads to write recommendations.

Read-only: journal, today's logs, and IBKR market/account data on its own
client ID (DAILY_REVIEW_CLIENT_ID). Never places or changes orders.

Output: analysis/daily/YYYY/MM/DD/facts_YYYY-MM-DD.md

    python -m tradingbot.run_daily_review [YYYY-MM-DD]
"""
from __future__ import annotations

import logging
import re
import sqlite3
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from tradingbot.config import settings
from tradingbot.paths import dated_dir

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
BACKTEST_EXPECT = {"pf": 1.56, "win_pct": 70.0, "avg_win": None, "avg_loss": None}  # ORB, scripts/orb_research_tests.py --fib


# ---------------------------------------------------------------- helpers ----

def read_log(name: str, day: date) -> list[str]:
    """Log lines for `day`, wrapped continuation lines joined (PowerShell
    wraps long lines and writes UTF-16)."""
    p = Path("logs") / f"{name}_{day.isoformat()}.log"
    if not p.exists():
        return []
    raw = p.read_bytes()
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "replace")
    out, cur = [], ""
    for ln in text.splitlines():
        if TS.match(ln):
            if cur:
                out.append(cur)
            cur = ln
        elif cur:
            cur += " " + ln.strip()
    if cur:
        out.append(cur)
    return out


def md_table(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "_(none)_\n"
    cols = list(df.columns)
    lines = ["| " + " | ".join(str(c) for c in cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join("" if pd.isna(v) else (f"{v:.2f}" if isinstance(v, float) else str(v))
                                       for v in r.values) + " |")
    return "\n".join(lines) + "\n"


def db() -> sqlite3.Connection:
    c = sqlite3.connect(settings.db_path)
    c.row_factory = sqlite3.Row
    return c


def q(sql: str, params=()) -> pd.DataFrame:
    with db() as c:
        return pd.read_sql(sql, c, params=params)


def utc_day_bounds(day: date) -> tuple[str, str]:
    return day.isoformat(), day.isoformat() + "T23:59:59"


# --------------------------------------------------------------- sections ----

def section_account(day: date, ib) -> str:
    out = ["## Account\n"]
    bal = q("SELECT date, net_liquidation FROM daily_balances ORDER BY date DESC LIMIT 6")
    if not bal.empty:
        bal["change"] = bal["net_liquidation"].diff(-1)
        out.append("Net liquidation (recorded by the 4:30pm snapshot):\n\n" + md_table(bal))
    ct = q("SELECT symbol, side, quantity, price, realized_pnl FROM closed_trades WHERE date=?", (day.isoformat(),))
    out.append(f"\nClosed fills today (IBKR fills; realized P&L total {ct['realized_pnl'].sum():.2f} — options that "
               f"expired worthless have no fill, so their cost is missing here):\n\n" + md_table(ct))
    out.append("\n" + strategy_pnl(day))
    if ib is not None:
        pos = [dict(symbol=p.contract.localSymbol or p.contract.symbol, type=p.contract.secType, qty=p.position,
                    avg_cost=round(p.avgCost, 2)) for p in ib.positions() if p.position != 0]
        out.append("\nOpen positions now:\n\n" + md_table(pd.DataFrame(pos)))
        orders = [dict(symbol=t.contract.localSymbol or t.contract.symbol, action=t.order.action,
                       qty=t.order.totalQuantity, type=t.order.orderType, aux=t.order.auxPrice,
                       lmt=t.order.lmtPrice, status=t.orderStatus.status) for t in ib.reqAllOpenOrders()]
        out.append("\nOpen orders now:\n\n" + md_table(pd.DataFrame(orders)))
    return "\n".join(out)


def strategy_pnl(day: date) -> str:
    """Realized P&L by strategy from the bots' own journals — complete trade
    results (e.g. a credit spread's long leg that expired worthless is
    included, unlike in IBKR's fill list), plus stock fills from IBKR."""
    d = day.isoformat()
    rows = []
    for name, sql in (
        ("ORB", "SELECT COALESCE(SUM(realized_pnl),0) FROM orb_positions WHERE status='closed' AND exit_date LIKE ?"),
        ("ORB credit spreads", "SELECT COALESCE(SUM(realized_pnl),0) FROM credit_orb_positions WHERE status='closed' "
                               "AND exit_ts LIKE ?"),
        ("Iron condor", "SELECT COALESCE(SUM(realized_pnl),0) FROM condor_positions WHERE status='closed' "
                        "AND exit_date LIKE ?"),
        ("Trend Join", "SELECT COALESCE(SUM(realized_pnl),0) FROM trend_join_positions WHERE status='closed' "
                       "AND exit_ts LIKE ?"),
    ):
        rows.append(dict(strategy=name, realized=float(q(sql, (d + "%",)).iloc[0, 0])))
    stocks = q("SELECT symbol, realized_pnl FROM closed_trades WHERE date=? AND symbol NOT LIKE '% %'", (d,))
    rows.append(dict(strategy="Stocks (RSI(2), MA, manual — IBKR fills)", realized=float(stocks["realized_pnl"].sum())))
    df = pd.DataFrame(rows)
    total = df["realized"].sum()
    return (f"Realized P&L by strategy (journals; complete trades) — total {total:.2f}:\n\n" + md_table(df)
            + "\nNote: journal exit times are UTC; a trade closed after 8pm ET would land on the next UTC date.\n")


def section_orb(day: date, model_trades: pd.DataFrame | None) -> str:
    out = ["## Strategy 3 — ORB (SPY 0DTE long options)\n",
           f"Live rules: confirm {settings.options_confirm_bars} closes beyond the 15m range, min range "
           f"{settings.options_min_range_pct:.2%}, VWAP side {settings.options_require_vwap_side}, entries before "
           f"{settings.options_last_entry_time}, step-up {settings.options_ladder_trigger_pct}/"
           f"{settings.options_ladder_step_pct}/{settings.options_ladder_trail_pct}, fib target "
           f"{settings.options_fib_target_ext}, VWAP exit {settings.options_vwap_exit}, re-entries "
           f"{settings.options_max_reentries}.\n"]
    tr = q("""SELECT id, symbol, strike, right, contracts, entry_date, entry_premium, peak_premium, exit_date,
              exit_premium, exit_reason, realized_pnl, range_low, range_high, entry_iv, model_iv
              FROM orb_positions WHERE entry_date LIKE ? ORDER BY id""", (day.isoformat() + "%",))
    if not tr.empty:
        for col in ("entry_date", "exit_date"):
            tr[col] = pd.to_datetime(tr[col], utc=True).dt.tz_convert(ET).dt.strftime("%H:%M")
        tr["peak_pct"] = (tr["peak_premium"] / tr["entry_premium"] - 1) * 100
        tr["iv_ratio"] = tr["entry_iv"] / tr["model_iv"]
    out.append("Live trades today:\n\n" + md_table(tr))
    log = read_log("orb", day)
    keep = [ln for ln in log if re.search(r"BUY_CALL|BUY_PUT|filtered out|paused|OPENED|CLOSED|closes (above|below)|"
                                           r"ERROR|WARNING|Fill implied vol", ln)]
    out.append("\nKey ORB log lines:\n\n```\n" + "\n".join(keep[:60]) + "\n```\n")
    if model_trades is not None:
        out.append("\nModel replay of today with the live rules (VIX-calibrated pricing; sized off a $100k account "
                   "like the backtest, so contracts and $ P&L differ from live — compare prices, timing and exit "
                   "reasons):\n\n" + md_table(model_trades))
    out.append(orb_rule_stats(day))
    return "\n".join(out)


def _stats_line(label: str, df: pd.DataFrame) -> str:
    if df.empty:
        return f"- {label}: no closed trades yet"
    pn = df["realized_pnl"].fillna(0)
    wins, losses = pn[pn > 0], pn[pn <= 0]
    pf = wins.sum() / -losses.sum() if losses.sum() else float("inf")
    return (f"- {label}: {len(pn)} trades, win {100 * len(wins) / len(pn):.0f}%, PF {pf:.2f}, net {pn.sum():.0f}, "
            f"avg win {wins.mean() if len(wins) else 0:.0f}, avg loss {losses.mean() if len(losses) else 0:.0f}")


def orb_rule_stats(day: date) -> str:
    """ORB stats split by rule version: only trades since
    OPTIONS_RULES_SINCE are comparable with the current backtest (earlier
    trades ran older rules and would make the comparison meaningless)."""
    since = settings.options_rules_since
    cur = q("""SELECT realized_pnl, entry_iv, model_iv FROM orb_positions WHERE status='closed' AND symbol='SPY'
               AND entry_date >= ? AND entry_date < ?""", (since, (day + pd.Timedelta(days=1)).isoformat()))
    old = q("SELECT realized_pnl FROM orb_positions WHERE status='closed' AND symbol='SPY' AND entry_date < ?", (since,))
    ratios = (cur["entry_iv"] / cur["model_iv"]).dropna() if not cur.empty else pd.Series(dtype=float)
    return "\n".join([
        "\nORB results by rule version:",
        _stats_line(f"Current rules (since {since}) — compare with the backtest: PF {BACKTEST_EXPECT['pf']}, "
                    f"win {BACKTEST_EXPECT['win_pct']:.0f}%", cur),
        _stats_line(f"Older rules (before {since}) — not comparable", old),
        f"- Fill IV / model IV median (current rules, {len(ratios)} fills): "
        f"{ratios.median() if len(ratios) else float('nan'):.2f} (pause threshold {settings.orb_iv_pause_ratio})\n",
    ])


def section_credit_orb(day: date) -> str:
    out = ["## Strategy 6 — ORB credit spreads (SPY 0DTE, paper experiment)\n"]
    pos = q("""SELECT id, status, direction, right, short_strike, long_strike, contracts, credit, exit_cost,
               exit_reason, realized_pnl, range_low, range_high FROM credit_orb_positions WHERE trade_date=?""",
            (day.isoformat(),))
    out.append(md_table(pos))
    if not pos.empty:
        marks = q("SELECT ts, spot, short_bid, short_ask, long_bid, long_ask, spread_mid FROM credit_orb_marks "
                  "WHERE position_id IN (%s) ORDER BY ts" % ",".join(str(i) for i in pos["id"]))
        if not marks.empty:
            marks["ts"] = pd.to_datetime(marks["ts"], utc=True).dt.tz_convert(ET).dt.strftime("%H:%M")
            out.append("\nLeg quote marks:\n\n" + md_table(marks))
    keep = [ln for ln in read_log("credit_orb", day) if re.search(r"breakout|SELL|OPENED|CLOSED|not filled|ERROR|WARNING", ln)]
    out.append("\nKey log lines:\n\n```\n" + "\n".join(keep[:40]) + "\n```\n")
    return "\n".join(out)


def section_other(day: date) -> str:
    start, end = utc_day_bounds(day)
    out = ["## Stock strategies (MA-crossover, RSI(2), Trend Join) and stops\n"]
    orders = q("SELECT ts, symbol, side, quantity, order_type, stop_price, status FROM orders WHERE ts >= ? AND ts <= ? "
               "ORDER BY ts", (start, end))
    out.append("Orders logged today (all bots):\n\n" + md_table(orders))
    sig = q("SELECT source, symbol, action, price, reason FROM signals WHERE ts >= ? AND ts <= ? AND action IN "
            "('BUY','EXIT') AND source IN ('run_rsi2_bot','run_ibkr_bot','run_trend_join_bot') ORDER BY ts", (start, end))
    out.append("\nBUY/EXIT signals today:\n\n" + md_table(sig))
    tj = q("SELECT symbol, entry_price, qty, remaining, initial_stop, stop_price, status, exit_reason, realized_pnl "
           "FROM trend_join_positions WHERE trade_date=?", (day.isoformat(),))
    out.append("\nTrend Join positions today:\n\n" + md_table(tj))
    pre = [ln for ln in read_log("trend_join", day) if "Prefilter" in ln]
    out.append("\nTrend Join prefilter:\n\n```\n" + "\n".join(pre[:8]) + "\n```\n")
    return "\n".join(out)


def section_health(day: date) -> str:
    out = ["## Health: errors and warnings in today's logs\n"]
    names = sorted(p.name[: -len(f"_{day.isoformat()}.log")] for p in Path("logs").glob(f"*_{day.isoformat()}.log"))
    for expected in ("orb", "credit_orb", "trend_join", "step_stop", "iron_condor", "rsi2", "ma_crossover", "expiry_guard"):
        if expected not in names:
            names.append(expected)
    for name in names:
        lines = read_log(name, day)
        bad = [ln for ln in lines if " ERROR " in ln or "Traceback" in ln or " WARNING " in ln]
        if not lines:
            out.append(f"- {name}: no log today")
        elif bad:
            out.append(f"- {name}: {len(bad)} warning/error lines, e.g.:\n```\n" + "\n".join(bad[:6]) + "\n```")
        else:
            out.append(f"- {name}: clean ({len(lines)} lines)")
    return "\n".join(out) + "\n"


def model_replay(day: date, ib) -> pd.DataFrame | None:
    """Today through the backtest with the live ORB settings and
    VIX-calibrated pricing, for comparison with the live trades."""
    from ib_async import Index

    from tradingbot.ibkr.data import fetch_intraday_bars
    from tradingbot.options.backtest import run_options_backtest
    from tradingbot.options.orb_strategy import OpeningRangeBreakoutStrategy
    from tradingbot.options.pricing import vix_vol_series

    spy = fetch_intraday_bars(ib, "SPY", 5, duration="2 D")
    vix_c = Index("VIX", "CBOE")
    ib.qualifyContracts(vix_c)
    from ib_async import util
    vdf = util.df(ib.reqHistoricalData(vix_c, "", "2 D", "5 mins", "TRADES", True, formatDate=2))
    vdf = vdf.set_index("date")
    vdf.index = pd.to_datetime(vdf.index, utc=True).tz_convert(ET)
    vol = vix_vol_series(spy.index, vdf["close"])
    h, m = settings.options_last_entry_time.split(":")
    r = run_options_backtest(
        symbol="SPY", bars=spy, strategy=OpeningRangeBreakoutStrategy(settings.options_opening_range_minutes, 5),
        starting_equity=100_000, risk_pct_per_trade=settings.options_risk_pct_per_trade,
        stop_loss_pct_of_premium=settings.options_stop_loss_pct_of_premium, profit_target_pct=None, dte_days=0,
        otm_pct=0.0, strike_increment=1.0, vol_window=20, iv_premium_multiplier=1.2, rate=0.04,
        exit_on_range_reentry=False, use_stop_loss=settings.options_use_stop_loss, vwap_exit=settings.options_vwap_exit,
        slippage_per_share=0.02, commission_per_contract=0.65, max_contracts=settings.options_max_contracts_per_trade,
        compound=False, ladder_trigger_pct=settings.options_ladder_trigger_pct or None,
        ladder_step_pct=settings.options_ladder_step_pct, ladder_trail_pct=settings.options_ladder_trail_pct,
        last_entry_time=time(int(h), int(m)), min_range_pct=settings.options_min_range_pct,
        require_vwap_side=settings.options_require_vwap_side, confirm_bars=settings.options_confirm_bars,
        fib_target_ext=settings.options_fib_target_ext or None, max_reentries=settings.options_max_reentries,
        vol_override=vol,
    )
    rows = [dict(right=t.right, entry=t.entry_time.strftime("%H:%M"), strike=t.strike, contracts=t.contracts,
                 entry_px=t.entry_premium,
                 peak=t.peak_premium, exit=t.exit_time.strftime("%H:%M") if t.exit_time is not None else "",
                 exit_px=t.exit_premium, reason=t.exit_reason, pnl=t.pnl)
            for t in r.trades if t.entry_time.date() == day]
    return pd.DataFrame(rows)


def main() -> None:
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.now(ET).date()
    ib = None
    model = None
    try:
        from ib_async import IB
        ib = IB()
        ib.connect(settings.ibkr_host, settings.ibkr_port, clientId=settings.daily_review_client_id, readonly=True)
        try:
            model = model_replay(day, ib)
        except Exception:
            logger.exception("model replay failed")
    except Exception:
        logger.exception("IBKR unavailable — facts limited to journal and logs")
        ib = None

    sections = [f"# Daily facts — {day.isoformat()} ({day.strftime('%A')})\n",
                "Generated after the close by run_daily_review.py. Read-only data for the daily review.\n"]
    for fn, args in ((section_account, (day, ib)), (section_orb, (day, model)), (section_credit_orb, (day,)),
                     (section_other, (day,)), (section_health, (day,))):
        try:
            sections.append(fn(*args))
        except Exception as e:
            logger.exception("section %s failed", fn.__name__)
            sections.append(f"## {fn.__name__}\n\n_section failed: {e}_\n")
    if ib is not None:
        ib.disconnect()

    out_dir = dated_dir(Path("analysis/daily"), day)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"facts_{day.isoformat()}.md"
    path.write_text("\n".join(sections), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
