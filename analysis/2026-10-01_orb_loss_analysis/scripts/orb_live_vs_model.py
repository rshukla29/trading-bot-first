"""Live ORB trades vs. what the backtest model does on the same days, and
model option prices vs. real fills. Read-only."""
from datetime import time

import pandas as pd
from ib_async import IB, Stock, util

from tradingbot.config import settings
from tradingbot.options.backtest import run_options_backtest, _time_to_expiry_years, TRADING_MINUTES_PER_DAY, TRADING_DAYS_PER_YEAR
from tradingbot.options.orb_strategy import OpeningRangeBreakoutStrategy
from tradingbot.options.pricing import bs_price, realized_volatility

SP = "analysis/2026-10-01_orb_loss_analysis/data/"

ib = IB()
ib.connect(settings.ibkr_host, settings.ibkr_port, clientId=94, readonly=True)
c = Stock("SPY", "SMART", "USD")
ib.qualifyContracts(c)
bars = util.df(ib.reqHistoricalData(c, "", "2 M", "5 mins", "TRADES", True, formatDate=2, timeout=300))
ib.disconnect()
bars = bars.set_index("date")[["open", "high", "low", "close", "volume"]]
bars.index = pd.to_datetime(bars.index, utc=True).tz_convert("America/New_York")
bars.to_csv(SP + "spy_5min_2026-08-03_to_2026-10-01.csv")
print("bars", bars.index[0], "..", bars.index[-1])

strategy = OpeningRangeBreakoutStrategy(15, 5)
common = dict(
    symbol="SPY", bars=bars, strategy=strategy, starting_equity=100_000, risk_pct_per_trade=0.01,
    stop_loss_pct_of_premium=0.30, profit_target_pct=None, dte_days=0, otm_pct=0.0, strike_increment=1.0,
    vol_window=20, iv_premium_multiplier=1.2, rate=0.04, exit_on_range_reentry=False, use_stop_loss=False,
    vwap_exit=True, slippage_per_share=0.02, commission_per_contract=0.65, max_contracts=10, compound=False,
    max_reentries=2, ladder_trigger_pct=0.5, ladder_step_pct=0.25, ladder_trail_pct=0.25,
)
res = run_options_backtest(**common)
model = pd.DataFrame([dict(day=str(t.entry_time.date()), right=t.right, entry_t=t.entry_time.strftime("%H:%M"),
                           strike=t.strike, entry=round(t.entry_premium, 2), peak=round(t.peak_premium, 2),
                           exit_t=t.exit_time.strftime("%H:%M") if t.exit_time is not None else None,
                           exit=round(t.exit_premium or 0, 2), reason=t.exit_reason, pnl=round(t.pnl or 0))
                      for t in res.trades if t.entry_time.date() >= pd.Timestamp("2026-09-14").date()])
pd.set_option("display.width", 250)
print("\n=== MODEL (current live rules) on the same days ===")
print(model.to_string(index=False))
print("model total since 9/14:", model.pnl.sum(), "| trades", len(model), "| winners", (model.pnl > 0).sum())

# Model price vs real fill at each live entry
live = pd.read_csv(SP + "live_orb_trades_2026-09-14_to_2026-10-01.csv")
live = live[live.symbol == "SPY"]
bpy = (TRADING_MINUTES_PER_DAY / 5) * TRADING_DAYS_PER_YEAR
vol = (realized_volatility(bars["close"], 20, bpy) * 1.2).shift(1)
print("\n=== Real entry fill vs model price at that moment ===")
for _, t in live.iterrows():
    ts = pd.Timestamp(f"{t.day} {t.entry_t}", tz="America/New_York").floor("5min")
    if ts not in bars.index:
        continue
    i = bars.index.get_loc(ts)
    spot = bars.close.iloc[i]
    expiry = pd.Timestamp(f"{t.day} 16:00", tz="America/New_York")
    mp = bs_price(spot, t.strike, _time_to_expiry_years(bars.index[i], expiry), 0.04, vol.iloc[i], t.right)
    print(f"{t.day} {t.entry_t} {t.right}{t.strike:.0f}: real {t.entry:.2f}  model {mp:.2f}  "
          f"(real/model {t.entry / mp:.2f}x)  spot {spot:.2f}")
