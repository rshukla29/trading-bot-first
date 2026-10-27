"""Backtest the Stock ORB rules (strategy/stock_orb.py) on the S&P 500.

Yahoo keeps ~60 days of 5-min bars, so that's the window. Each day: every
symbol's first trigger (5-min close above the 15-min opening range high,
9:45-15:30), taken in time order and, within the same bar, by dollars
traded so far; max 3 trades a day, $50 risk per trade (capped at a $4,000
position), $150 daily stop-loss budget; stop at the range midpoint,
target at 1R / 1.5R / 2R, flat by 15:55. Costs: $0.01/share slippage on
market fills (entry, stop, flatten) and IBKR fixed commission ($0.005/share,
$1 minimum) on both sides.

Sources of the settings:
  - 3 trades/day, $50/trade, $150/day, $4,000 max position: the pack's
    trader-risk defaults (max_trades_per_day, max_loss_per_trade_usd,
    max_daily_loss_usd, max_order_usd), unscaled.
  - Commission and slippage: assumptions of this repo (IBKR fixed-rate
    pricing; $0.01/share on market fills). The pack has no cost model.

    python scripts/stock_orb_backtest.py            # downloads, caches data/stock_orb_5m.pkl
    python scripts/stock_orb_backtest.py --refresh  # re-download
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

from tradingbot.sp500_scanner import fetch_sp500_constituents
from tradingbot.strategy.stock_orb import find_trigger, plan_trade, regular_session, share_quantity, simulate_exit

CACHE = Path("data/stock_orb_5m.pkl")
MAX_LOSS, MAX_ORDER, DAILY_LOSS, MAX_TRADES = 50.0, 4_000.0, 150.0, 3
SLIP = 0.01


def commission(shares: int) -> float:
    return max(1.0, 0.005 * shares)


def load(refresh: bool) -> dict[str, pd.DataFrame]:
    if CACHE.exists() and not refresh:
        return pd.read_pickle(CACHE)
    symbols = fetch_sp500_constituents()["symbol"].tolist()
    raw = yf.download(symbols, period="60d", interval="5m", group_by="ticker", auto_adjust=False,
                      prepost=False, threads=True, progress=False)
    out = {}
    for s in symbols:
        if s not in raw.columns.get_level_values(0):
            continue
        df = raw[s].rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
        if df.empty:
            continue
        df.index = df.index.tz_convert("America/New_York")
        out[s] = regular_session(df)
    CACHE.parent.mkdir(exist_ok=True)
    pd.to_pickle(out, CACHE)
    return out


def all_triggers(bars: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for sym, df in bars.items():
        for d, day in df.groupby(df.index.date):
            if day["close"].iloc[0] < 5:
                continue
            trig = find_trigger(day)
            if trig is not None:
                rows.append(dict(symbol=sym, date=d, ts=trig.ts, entry=trig.entry, or_high=trig.or_high,
                                 or_low=trig.or_low, dollar_volume=trig.dollar_volume, day=day))
    return pd.DataFrame(rows)


def run(triggers: pd.DataFrame, target_r: float, cap: bool = True) -> pd.DataFrame:
    trades = []
    for d, group in triggers.sort_values(["ts", "dollar_volume"], ascending=[True, False]).groupby("date"):
        taken, stop_budget = 0, 0.0
        for _, t in group.iterrows():
            if cap and taken >= MAX_TRADES:
                break
            plan = plan_trade(t.entry + SLIP, t.or_high, t.or_low, target_r)
            if plan is None:
                continue
            qty = share_quantity(plan.entry, plan.stop, MAX_LOSS, MAX_ORDER)
            if qty <= 0:
                continue
            risk_dollars = qty * plan.risk
            if cap and stop_budget + risk_dollars > DAILY_LOSS:
                continue
            outcome, px, _ = simulate_exit(t.day[t.day.index > t.ts], plan)
            exit_px = px if outcome == "target" else px - SLIP
            pnl = (exit_px - plan.entry) * qty - commission(qty) * 2
            trades.append(dict(date=d, symbol=t.symbol, time=t.ts.strftime("%H:%M"), entry=plan.entry,
                               stop=plan.stop, target=plan.target, qty=qty, outcome=outcome, exit=exit_px,
                               pnl=pnl, r=(exit_px - plan.entry) / plan.risk))
            taken += 1
            stop_budget += risk_dollars
    return pd.DataFrame(trades)


def stats(t: pd.DataFrame) -> dict:
    wins, losses = t[t.pnl > 0].pnl, t[t.pnl <= 0].pnl
    equity = t.pnl.cumsum()
    return dict(trades=len(t), win_pct=round((t.pnl > 0).mean() * 100, 1),
                pf=round(wins.sum() / -losses.sum(), 2) if losses.sum() else float("inf"),
                net=round(t.pnl.sum()), avg_win=round(wins.mean()) if len(wins) else 0,
                avg_loss=round(losses.mean()) if len(losses) else 0, avg_r=round(t.r.mean(), 3),
                target=int((t.outcome == "target").sum()), stop=int((t.outcome == "stop").sum()),
                flat=int((t.outcome == "flat").sum()),
                max_dd=round((equity - equity.cummax()).min()),
                green_days=f"{(t.groupby('date').pnl.sum() > 0).sum()}/{t.date.nunique()}")


def main() -> None:
    bars = load("--refresh" in sys.argv)
    days = sorted({d for df in bars.values() for d in df.index.date})
    print(f"{len(bars)} symbols, {len(days)} sessions ({days[0]} to {days[-1]})")
    trig = all_triggers(bars)
    print(f"{len(trig)} symbol-days triggered (of ~{len(bars) * len(days)})\n")

    pd.set_option("display.width", 200)
    rows = {}
    for r in (1.0, 1.5, 2.0):
        rows[f"{r:g}R  live rules (3/day)"] = stats(run(trig, r))
        rows[f"{r:g}R  every trigger (edge check)"] = stats(run(trig, r, cap=False))
    print(pd.DataFrame(rows).T.to_string())

    best = max((1.0, 1.5, 2.0), key=lambda r: rows[f"{r:g}R  live rules (3/day)"]["net"])
    t = run(trig, best)
    t["hour"] = t.time.str[:2]
    print(f"\nBest live target: {best:g}R — by entry hour:")
    print(t.groupby("hour").agg(trades=("pnl", "size"), win_pct=("pnl", lambda s: round((s > 0).mean() * 100)),
                                net=("pnl", lambda s: round(s.sum())), avg_r=("r", lambda s: round(s.mean(), 2))).to_string())
    t.to_csv(Path("data") / "stock_orb_backtest_trades.csv", index=False)
    print("\nTrades: data/stock_orb_backtest_trades.csv")


if __name__ == "__main__":
    main()
