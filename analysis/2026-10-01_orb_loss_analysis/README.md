# ORB loss analysis — 2026-10-01

**Question:** why did the live ORB (opening range breakout, 0DTE options)
strategy lose money from 2026-09-14 to 2026-10-01, and what should change?

**Answer in one line:** the VWAP exit caused all the losses, and the
backtest we had tuned on priced 0DTE options too cheaply, so it overstated
the edge. With realistic option prices the edge is real but thin, and it
appears only without the VWAP exit.

## 1. Live results (paper account, 2026-09-14 → 2026-10-01)

16 closed trades, 6 winners, **−$1,796** (journal total −$1,852, which
includes one 3:55pm trade the log never showed closing). Per-trade detail:
[data/live_orb_trades_2026-09-14_to_2026-10-01.csv](data/live_orb_trades_2026-09-14_to_2026-10-01.csv).

| Group | Trades | P&L |
|---|---|---|
| Closed by the VWAP exit | 8 | **−$4,630** (avg −$579) |
| Closed by the step-up lock or profit target | 6 | **+$2,964** |
| Never worked (never up more than 10%) | 8 | −$3,010; average drop of 45% before the VWAP exit fired |
| Up at some point, ended as a loss | 2 | −$1,750 (SPY 766P on 9/28 peaked +162%; SPY 760P on 10/1 peaked +21%) |
| First entry per direction | 12 | +$214 |
| Re-entries | 4 | −$2,010 |
| Entries 9:45–10:15 | 6 | +$1,374 |
| Entries 10:15–12:00 | 7 | −$3,310 |
| IWM | 2 | −$790 |

**Pattern:** the VWAP exit waits for SPY to close back across VWAP. By then
a 0DTE option has usually lost about half its value, so the exit cuts losses
only after they're already large.

## 2. The backtest priced options too cheaply

Implied volatility backed out of each real SPY fill
([scripts/orb_iv.py](scripts/orb_iv.py)):

- **Market:** about 15–22% intraday, 38–45% in the last 20 minutes.
- **Old backtest assumption:** realized 5-min volatility × 1.2, anywhere
  from 5% to 29%. Median 1.4× too cheap; 3–8× too cheap near the close.

**Calibrated replacement** ([scripts/orb_realistic.py](scripts/orb_realistic.py)):
implied vol ≈ **VIX × 1.07** with 60+ minutes left, rising linearly to
about **2.9×** that at the 4:00pm close. Fit on 14 real SPY fills; needs
`data/vix_5min.csv` (4.5 years of 5-min VIX from IBKR) and
`data/spy_5min.csv` in the repo's gitignored `data/` folder.

| Setup that ran live on 2026-09-29 → 10-01 | Profit factor | Net (4 yrs, $100k, 10-contract cap) |
|---|---|---|
| Old pricing | 1.30 | +$161k |
| **Realistic pricing** | **1.14** | **+$86k** |

Profit factor = gross profits ÷ gross losses; 1.0 is breakeven.

## 3. What works under realistic pricing

SPY, Oct 2022 → Oct 2026, $0.02/share slippage, $0.65/contract
commission. Full output: [data/realistic_pricing_results.txt](data/realistic_pricing_results.txt),
[data/robustness_results.txt](data/robustness_results.txt).

| Setup | PF | Net | Win % | Max DD | PF at $0.05 slippage | PF if options cost 25% more |
|---|---|---|---|---|---|---|
| **Step-up lock + 3:55pm only, entries before 12:00** | **1.40** | +$115k | 61% | −6.2% | 1.26 | 0.99 |
| Same, all-day entries | 1.39 | +$120k | 60% | −6.0% | 1.24 | 0.99 |
| Same, entries before 10:15 / 10:30 / 11:00 | 1.40–1.44 | +$101k–111k | 62% | −5.2 to −6.0% | 1.25–1.27 | 0.98–1.00 |
| Same, step-up starting at +100% | 1.33 | +$108k | 52% | −8.0% | 1.25 | **1.05** |
| Step-up + VWAP exit, entries before 10:30 | 0.99 | −$3k | 36% | −25.5% | 0.86 | 0.86 |
| Adding a −30% or −50% premium stop | 0.98–0.99 | ≈ −$3k | 34–36% | −26 to −29% | — | — |
| VWAP exit only | 1.35 | +$144k | 24% | −9.6% | — | — |
| Original rules (−30% stop, +50% target, range re-entry) | 0.92–0.94 | −$15k | 31% | −28% | — | — |

**Conclusions**

- **Removing the VWAP exit is the fix.** Results hold at 1.39–1.44 across
  every entry cutoff, so it isn't a lucky parameter. Independent live
  evidence (section 1) points the same way.
- **Re-entry stops mattering** without the VWAP exit; it rarely triggers.
- **Premium stops don't help** under any pricing.
- **Fragility:** if real option prices run about 25% above the calibration,
  the edge disappears. The +100% step-up start is the most resistant.
- **Overfitting caution:** many variants were tested on the same history.
  Expect live results below the backtest. Judge after 6–8 weeks of paper
  trading.

## 4. Bugs and gaps found

| Issue | Status at time of analysis |
|---|---|
| Live judged unfinished 5-min bars and missed most breakouts (took 4 of 16 signals 9/11–9/25) | Fixed 2026-09-28 |
| Partly-filled sell retried at full size, leaving short 11 IWM and 2 SPY puts | Fixed 2026-09-28 |
| No entry cutoff: entries at 3:55pm on 9/18 and 9/30, sold seconds later | Open → change 2 |
| Restart forgets the peak premium (9/28 put's +162% peak wasn't locked in) | Open → change 4 |
| IWM had no edge in any variant | Dropped 2026-09-29 |

## 5. Changes decided

1. Remove the VWAP exit for ORB. Exits: step-up lock (+50% → stop +25%,
   then every 25% step) or the 3:55pm flatten. Max loss per trade = premium.
2. No new ORB entries after 12:00.
3. Re-entry off.
4. Persist the peak premium in the journal so restarts keep the step-up.
5. ORB backtests use VIX-calibrated pricing by default.
6. Track each fill's implied vol against the VIX; pause ORB if fills run
   more than ~25% above the estimate.

## Reproduce

From the repo root:

```
python analysis/2026-10-01_orb_loss_analysis/scripts/orb_paths.py         # per-trade paths from logs/orb_*.log
python analysis/2026-10-01_orb_loss_analysis/scripts/orb_iv.py            # real vs model implied vol
python analysis/2026-10-01_orb_loss_analysis/scripts/orb_live_vs_model.py # live vs model trades (refetches SPY bars from IBKR)
python analysis/2026-10-01_orb_loss_analysis/scripts/orb_realistic.py     # 4-year re-test, VIX-calibrated pricing
```
