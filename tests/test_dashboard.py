import json
import re
import sqlite3
from datetime import date

from tradingbot.dashboard import bot_health, collect, demojibake, markdown, render
from tradingbot.journal import Journal
from tradingbot.strategy_pnl import strategy_pnl


def _journal(tmp_path) -> str:
    db = str(tmp_path / "j.db")
    Journal(db).record_daily_balance("2026-10-29", 1_000_100.0)
    Journal(db).record_daily_balance("2026-10-30", 1_000_400.0)
    conn = sqlite3.connect(db)
    conn.execute("""INSERT INTO orb_positions (symbol, entry_date, expiry, strike, right, conid, entry_premium,
                    contracts, range_low, range_high, status, exit_date, exit_premium, exit_reason, realized_pnl)
                    VALUES ('SPY', '2026-10-30T14:20:00+00:00', '20261030', 758, 'P', 1, 1.62, 10, 759, 761,
                    'closed', '2026-10-30T14:25:00+00:00', 2.26, 'fib_target', 640)""")
    conn.execute("""INSERT INTO trend_join_positions (symbol, trade_date, entry_ts, entry_price, qty, remaining,
                    initial_stop, stop_price, status, exit_ts, exit_reason, realized_pnl)
                    VALUES ('VLTO', '2026-10-30', '2026-10-30T16:35:00+00:00', 114.67, 17, 0, 106.42, 106.42,
                    'closed', '2026-10-30T19:51:00+00:00', 'eod_flatten', -49.47)""")
    conn.execute("""INSERT INTO condor_positions (symbol, entry_date, expiry, call_short_strike, call_long_strike,
                    put_short_strike, put_long_strike, call_short_conid, call_long_conid, put_short_conid,
                    put_long_conid, credit_received, contracts, status)
                    VALUES ('SPY', '2026-10-26T19:48:00+00:00', '20261204', 802, 807, 739, 735, 1, 2, 3, 4, 1.19, 10,
                    'open')""")
    # An RSI(2) stock sold in two lots, and Trend Join's own sale (counted once, by its journal).
    conn.execute("INSERT INTO orders (ts, symbol, side, quantity, order_type, broker, status) "
                 "VALUES ('2026-10-27T20:30:00+00:00', 'COR', 'BUY', 12, 'MKT', 'IBKR', 'filled')")
    conn.execute("INSERT INTO signals (ts, symbol, action, price, reason, source) "
                 "VALUES ('2026-10-27T20:29:30+00:00', 'COR', 'BUY', 330, 'rsi2', 'run_rsi2_bot')")
    for i, (sym, qty, px, pnl) in enumerate([("COR", 5, 334.32, 30.0), ("COR", 7, 334.32, 48.1),
                                             ("VLTO", 17, 111.76, -51.5)]):
        conn.execute("INSERT INTO closed_trades (exec_id, date, symbol, side, quantity, price, realized_pnl, ts, "
                     "commission) VALUES (?, '2026-10-30', ?, 'SLD', ?, ?, ?, '2026-10-30T20:30:00+00:00', 1.0)",
                     (f"e{i}", sym, qty, px, pnl))
    conn.commit()
    conn.close()
    return db


def test_trades_match_the_reports_strategy_pnl(tmp_path):
    db = _journal(tmp_path)
    data = collect(db, tmp_path / "reports", tmp_path / "analysis", tmp_path / "logs", tmp_path / "reports",
                   1_000_000.0, date(2026, 10, 30))
    by = {}
    for t in data["trades"]:
        by[t["strategy"]] = round(by.get(t["strategy"], 0) + t["pnl"], 2)
    expected = {r.strategy: r.realized for r in strategy_pnl(db, date(2000, 1, 1), date(2100, 1, 1)).itertuples()
                if r.trades}
    assert by == expected == {"ORB (SPY 0DTE options)": 640.0, "Trend Join": -49.47, "RSI(2)": 78.1}
    cor = next(t for t in data["trades"] if t["symbol"] == "COR")
    assert cor["qty"] == 12 and cor["exit"] == 334.32          # two lots, one trade
    assert [p["strategy"] for p in data["open"]] == ["Iron condor"]
    assert data["balances"][-1] == {"date": "2026-10-30", "value": 1_000_400.0}


def test_page_embeds_the_data_safely(tmp_path):
    db = _journal(tmp_path)
    data = collect(db, tmp_path / "reports", tmp_path / "analysis", tmp_path / "logs", tmp_path / "reports",
                   1_000_000.0, date(2026, 10, 30))
    data["trades"][0]["reason"] = "</script><script>alert(1)</script>"
    page = render(data)
    assert page.count("</script>") == page.count("<script>") == 1
    embedded = json.loads(re.search(r"const D = (\{.*?\});\n", page, re.S).group(1))
    assert embedded["trades"][0]["reason"] == "</script><script>alert(1)</script>"


def test_markdown_escapes_and_renders_tables_and_lists():
    out = markdown("## Summary\nP&L was **+$5** <b>x</b>\n\n| a | b |\n|---|---|\n| 1 | `2` |\n\n- one\n  - two\n- three\n")
    assert "<h3>Summary</h3>" in out and "&lt;b&gt;x&lt;/b&gt;" in out and "<strong>+$5</strong>" in out
    assert "<td>1</td><td><code>2</code></td>" in out
    assert out.count("<ul>") == 2 and "three" in out


def test_demojibake_repairs_code_page_437_dashes():
    assert demojibake("note ΓÇö not a bug") == "note — not a bug"
    assert demojibake("already fine — and café") == "already fine — and café"


def test_bot_health_separates_ibkr_notices_from_errors(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "trend_join_2026-11-02.log").write_text(
        "2026-11-02 15:51:38,149 ERROR Error 202, reqId 8995: Order Canceled - reason:\n"
        "2026-11-02 09:26:40,253 ERROR positions request timed out\n", encoding="utf-8")
    (logs / "iron_condor_2026-11-02.log").write_bytes(
        "2026-11-02 15:45:19,164 ERROR SPY: error during condor management, skipping\nTraceback (...)\n"
        .encode("utf-16"))
    h = bot_health(logs, date(2026, 11, 2))
    tj, ic = h["cells"]["2026-11-02"]["trend_join"], h["cells"]["2026-11-02"]["iron_condor"]
    assert (tj["errors"], tj["warnings"]) == (0, 2)
    assert ic["errors"] == 1 and ic["first_error"] == "SPY: error during condor management, skipping"
