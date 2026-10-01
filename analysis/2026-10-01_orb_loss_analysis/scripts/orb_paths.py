"""Parse ORB logs into per-trade premium paths: entry, peak (MFE), low (MAE),
time to peak, exit. Read-only analysis."""
import re
import sqlite3
import subprocess
from pathlib import Path

import pandas as pd

LOGS = sorted(Path("logs").glob("orb_2026-*.log"))
TS = re.compile(r"^(2026-\d\d-\d\d \d\d:\d\d:\d\d)")
OPENED = re.compile(r"(\w+): OPENED ORB \d+ ([\d.]+)([CP]) x(\d+), actual entry premium=([\d.]+)")
HOLD = re.compile(r"(\w+): ORB position holding \(premium ([\d.]+) vs entry ([\d.]+)")
CLOSED = re.compile(r"(\w+): CLOSED ORB position \((\w+)\), actual exit premium=([\d.]+), pnl=(-?[\d.]+)")


def read_log(p: Path) -> list[str]:
    raw = p.read_bytes()
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "replace")
    lines, cur = [], ""
    for ln in text.splitlines():
        if TS.match(ln):
            if cur:
                lines.append(cur)
            cur = ln
        else:
            cur += " " + ln.strip()
    if cur:
        lines.append(cur)
    return lines


rows, open_tr = [], {}
for p in LOGS:
    for ln in read_log(p):
        if not TS.match(ln):
            continue
        ts = TS.match(ln).group(1)
        if m := OPENED.search(ln):
            sym = m.group(1)
            open_tr[sym] = dict(day=ts[:10], symbol=sym, strike=float(m.group(2)), right=m.group(3),
                                qty=int(m.group(4)), entry_t=ts[11:16], entry=float(m.group(5)), path=[])
        elif (m := HOLD.search(ln)) and m.group(1) in open_tr:
            open_tr[m.group(1)]["path"].append((ts[11:19], float(m.group(2))))
        elif (m := CLOSED.search(ln)) and m.group(1) in open_tr:
            t = open_tr.pop(m.group(1))
            prem = [x for _, x in t["path"]] + [float(m.group(3))]
            peak_i = max(range(len(prem)), key=lambda i: prem[i])
            t.update(exit_t=ts[11:16], reason=m.group(2), exit=float(m.group(3)), pnl=float(m.group(4)),
                     peak=max(prem + [t["entry"]]), low=min(prem + [t["entry"]]),
                     peak_t=(t["path"][peak_i][0][:5] if peak_i < len(t["path"]) else ts[11:16]),
                     ticks=len(t["path"]))
            rows.append(t)

df = pd.DataFrame(rows).drop(columns=["path"])
df["peak_pct"] = (df.peak / df.entry - 1) * 100
df["low_pct"] = (df.low / df.entry - 1) * 100
df["exit_pct"] = (df.exit / df.entry - 1) * 100
df["peak_$"] = (df.peak - df.entry) * df.qty * 100
df["gave_back_$"] = df["peak_$"] - df.pnl
pd.set_option("display.width", 250)
print(df[["day", "symbol", "strike", "right", "entry_t", "entry", "peak", "peak_t", "low", "exit", "exit_t",
          "reason", "pnl", "peak_pct", "low_pct", "peak_$", "gave_back_$"]].round(1).to_string(index=False))
df.to_csv("analysis/2026-10-01_orb_loss_analysis/data/"
          "live_orb_trades_2026-09-14_to_2026-10-01.csv", index=False)
print("\ntrades", len(df), "| winners", (df.pnl > 0).sum(), "| total pnl", df.pnl.sum())
print("sum of peak $ (best possible exit):", round(df["peak_$"].clip(lower=0).sum()),
      "| trades that were ever up >=25%:", (df.peak_pct >= 25).sum(), "| of those ended as losers:",
      ((df.peak_pct >= 25) & (df.pnl < 0)).sum())
