"""Re-test ORB variants with option prices calibrated to real 0DTE fills
(VIX-based implied vol + end-of-day ramp) instead of realized 5-min vol."""
from datetime import time

import numpy as np
import pandas as pd

from tradingbot.options.backtest import run_options_backtest, _time_to_expiry_years
from tradingbot.options.orb_strategy import OpeningRangeBreakoutStrategy
from tradingbot.options.pricing import bs_price

SP = "analysis/2026-10-01_orb_loss_analysis/data/"
ET = "America/New_York"


def load(path):
    df = pd.read_csv(path, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(ET)
    return df


spy = pd.concat([load("data/spy_5min.csv"), load(SP + "spy_5min_2026-08-03_to_2026-10-01.csv")]).sort_index()
spy = spy[~spy.index.duplicated(keep="last")][["open", "high", "low", "close", "volume"]]
vix = load("data/vix_5min.csv")["close"].reindex(spy.index).ffill() / 100

# minutes to the 16:00 close at each bar's close
bar_close = spy.index + pd.Timedelta(minutes=5)
mins_left = pd.Series(((bar_close.normalize() + pd.Timedelta(hours=16)) - bar_close).total_seconds() / 60,
                      index=spy.index).clip(lower=1)


def implied_vol(price, spot, strike, t, right):
    lo, hi = 0.01, 5.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, t, 0.04, mid, right) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


# ---- calibrate on the real SPY ORB fills -------------------------------------
live = pd.read_csv(SP + "live_orb_trades_2026-09-14_to_2026-10-01.csv")
live = live[live.symbol == "SPY"]
pts = []
for _, t in live.iterrows():
    bar_ts = pd.Timestamp(f"{t.day} {t.entry_t}", tz=ET).floor("5min") - pd.Timedelta(minutes=5)
    if bar_ts not in spy.index:
        continue
    now = bar_ts + pd.Timedelta(minutes=5)
    t_y = _time_to_expiry_years(now, pd.Timestamp(f"{t.day} 16:00", tz=ET))
    iv = implied_vol(t.entry, float(spy.close[bar_ts]), t.strike, t_y, t.right)
    pts.append((float(mins_left[bar_ts]), iv / float(vix[bar_ts])))
pts = pd.DataFrame(pts, columns=["mins_left", "ratio"])
a = float(pts[pts.mins_left >= 60].ratio.median())
late = pts[pts.mins_left < 60]
# ramp(m) = 1 + b * (60 - m) / 60 for the last hour, fit on the late fills
b = float(((late.ratio / a - 1) / ((60 - late.mins_left) / 60)).median()) if len(late) else 0.0
print("calibration points:\n", pts.round(2).to_string(index=False))
print(f"\nimplied vol = VIX x {a:.2f} (>=60 min left), ramping up by x{1 + b:.2f} at the close (b={b:.2f})")

ramp = 1 + b * ((60 - mins_left).clip(lower=0) / 60)
vol_override = vix * a * ramp

strategy = OpeningRangeBreakoutStrategy(15, 5)
common = dict(
    symbol="SPY", bars=spy, strategy=strategy, starting_equity=100_000, risk_pct_per_trade=0.01,
    stop_loss_pct_of_premium=0.30, profit_target_pct=None, dte_days=0, otm_pct=0.0, strike_increment=1.0,
    vol_window=20, iv_premium_multiplier=1.2, rate=0.04, exit_on_range_reentry=False, use_stop_loss=False,
    vwap_exit=True, slippage_per_share=0.02, commission_per_contract=0.65, max_contracts=10, compound=False,
)
ladder = dict(ladder_trigger_pct=0.5, ladder_step_pct=0.25, ladder_trail_pct=0.25)
V = {
    "live_now (vwap+ladder+re2)": dict(**ladder, max_reentries=2),
    "  no re-entry": dict(**ladder),
    "  no re-entry, entries <10:30": dict(**ladder, last_entry_time=time(10, 30)),
    "  no re-entry, entries <11:00": dict(**ladder, last_entry_time=time(11, 0)),
    "  no re-entry, entries <15:00": dict(**ladder, last_entry_time=time(15, 0)),
    "  <10:30 + -30% stop": dict(**ladder, last_entry_time=time(10, 30), use_stop_loss=True),
    "  <10:30 + -50% stop": dict(**ladder, last_entry_time=time(10, 30), use_stop_loss=True, stop_loss_pct_of_premium=0.5),
    "  <10:30, range exit not VWAP": dict(**ladder, last_entry_time=time(10, 30), vwap_exit=False, exit_on_range_reentry=True),
    "  <10:30, ladder + EOD only": dict(**ladder, last_entry_time=time(10, 30), vwap_exit=False),
    "vwap only (no ladder)": dict(),
    "original (-30%, +50%, range)": dict(vwap_exit=False, use_stop_loss=True, profit_target_pct=0.5, exit_on_range_reentry=True),
    "original <10:30": dict(vwap_exit=False, use_stop_loss=True, profit_target_pct=0.5, exit_on_range_reentry=True,
                             last_entry_time=time(10, 30)),
}


def max_dd(curve):
    peak, worst = -np.inf, 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        worst = min(worst, eq / peak - 1)
    return worst


rows, halves = [], {}
for name, kw in V.items():
    for label, vo in (("realistic", vol_override), ("old model", None)):
        if label == "old model" and name not in ("live_now (vwap+ladder+re2)",):
            continue
        r = run_options_backtest(**{**common, **kw}, vol_override=vo)
        cl = r.closed_trades
        pn = [t.pnl for t in cl]
        w = [p for p in pn if p > 0]
        gl = -sum(p for p in pn if p <= 0)
        rows.append(dict(variant=name, pricing=label, trades=len(cl), win=round(100 * len(w) / max(len(cl), 1), 1),
                         net=round(sum(pn)), pf=round(sum(w) / gl, 2) if gl else np.inf, dd=round(100 * max_dd(r.equity_curve), 1)))
        if label == "realistic":
            per = {}
            for t in cl:
                k = f"{t.exit_time.year}-H{1 if t.exit_time.month <= 6 else 2}"
                per[k] = per.get(k, 0) + t.pnl
            halves[name.strip()[:28]] = per
pd.set_option("display.width", 250)
print(pd.DataFrame(rows).to_string(index=False))
print("\nNet P&L by half-year (realistic pricing):")
print(pd.DataFrame(halves).sort_index().round(0).fillna(0).astype(int).to_string())
