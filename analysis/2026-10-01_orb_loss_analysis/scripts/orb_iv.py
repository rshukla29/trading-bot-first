"""Implied vol of each real ORB fill vs. the vol the backtest assumes."""
import pandas as pd

from tradingbot.options.backtest import _time_to_expiry_years, TRADING_MINUTES_PER_DAY, TRADING_DAYS_PER_YEAR
from tradingbot.options.pricing import bs_price, realized_volatility

SP = "analysis/2026-10-01_orb_loss_analysis/data/"
bars = pd.read_csv(SP + "spy_5min_2026-08-03_to_2026-10-01.csv", index_col=0)
bars.index = pd.to_datetime(bars.index, utc=True).tz_convert("America/New_York")
bpy = (TRADING_MINUTES_PER_DAY / 5) * TRADING_DAYS_PER_YEAR
rv = realized_volatility(bars["close"], 20, bpy).shift(1)


def implied_vol(price, spot, strike, t, right):
    lo, hi = 0.01, 5.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, t, 0.04, mid, right) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


live = pd.read_csv(SP + "live_orb_trades_2026-09-14_to_2026-10-01.csv")
live = live[live.symbol == "SPY"]
rows = []
for _, t in live.iterrows():
    entry_ts = pd.Timestamp(f"{t.day} {t.entry_t}", tz="America/New_York")
    bar_ts = entry_ts.floor("5min") - pd.Timedelta(minutes=5)  # the completed bar the entry acted on
    if bar_ts not in bars.index:
        continue
    i = bars.index.get_loc(bar_ts)
    spot = float(bars.close.iloc[i])
    now = bar_ts + pd.Timedelta(minutes=5)
    t_y = _time_to_expiry_years(now, pd.Timestamp(f"{t.day} 16:00", tz="America/New_York"))
    model_vol = rv.iloc[i] * 1.2
    model_px = bs_price(spot, t.strike, t_y, 0.04, model_vol, t.right)
    iv = implied_vol(t.entry, spot, t.strike, t_y, t.right)
    rows.append(dict(day=t.day, entry_t=t.entry_t, opt=f"{t.right}{t.strike:.0f}", spot=round(spot, 2),
                     real=t.entry, model=round(model_px, 2), real_iv=round(iv, 3), model_iv=round(model_vol, 3),
                     iv_ratio=round(iv / model_vol, 2), mins_left=round(t_y * 365.25 * 24 * 60)))
df = pd.DataFrame(rows)
pd.set_option("display.width", 200)
print(df.to_string(index=False))
print("\nmedian real IV / model IV:", df.iv_ratio.median(), "| mean:", round(df.iv_ratio.mean(), 2))
