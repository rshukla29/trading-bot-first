# trading-bot

Automated trading bots for Interactive Brokers (via `ib_async`): five
independent strategies covering stocks and SPY/IWM options, plus daily,
weekly and monthly P&L reports. **Paper trading only (account `DU…`) until
you explicitly opt into live money.**

## Strategies at a glance

| # | Strategy | Instrument | Style | Schedule (ET, Mon–Fri) | Entrypoint |
|---|---|---|---|---|---|
| 1 | MA-crossover + consolidation retest | Stocks — `WATCHLIST` (19 names) | Trend-following, daily bars, swing (days–weeks) | 4:30pm, right after RSI(2) — `RSI2TradingBot` | `run_ibkr_bot` |
| 2 | RSI(2) mean-reversion | Stocks — `RSI2_WATCHLIST` (10) + IBKR top movers + S&P 500 yfinance screen (scans not on Fridays) | Dip-buying, daily bars, swing (days) | 4:30pm — `RSI2TradingBot` | `run_rsi2_bot` |
| 3 | Opening Range Breakout (ORB) | SPY + IWM 0DTE calls/puts | Intraday options | 9:29am–4:00pm — `ORBTradingBot` | `run_orb_bot` |
| 4 | Iron condor | SPY options, ~45 DTE | Premium selling, multi-week | 3:45pm — `IronCondorTradingBot` | `run_iron_condor_bot` |
| 5 | Trend Join Long | S&P 500 gap-up stocks | Intraday gap-and-go | 9:50am–4:00pm — `TrendJoinTradingBot` | `run_trend_join_bot` |
| — | Step-stop manager (not a strategy) | MA-crossover + RSI(2) positions | Raises stops in 10% profit steps | 9:25am–4:00pm — `StepStopTradingBot` | `run_step_stop_bot` |

Run any of them manually with `python -m tradingbot.<entrypoint>`.

### Daily schedule (ET, Mon–Fri — Windows Task Scheduler)

| Time | Task | What happens |
|---|---|---|
| 9:00am | `SnapshotPremarketBot` | Premarket report |
| 9:25am → 4:00pm | `StepStopTradingBot` | Stop audit, then raises stock stops every ~60s |
| 9:29am → 4:00pm | `ORBTradingBot` | SPY + IWM opening-range breakout, VWAP exit; flat by 3:55pm |
| 9:50am → 4:00pm | `TrendJoinTradingBot` | Gap scans 9:55–12:55, entries 10:05–3:30, flat by 3:51pm |
| 3:45pm | `IronCondorTradingBot` | Manage / open SPY iron condors |
| 4:30pm | `RSI2TradingBot` | RSI(2), then MA-crossover (stop audit first in each) |
| 4:30pm | `SnapshotPostmarketBot` | Postmarket report (+ weekly on Fridays, monthly on the last trading day) |

### Profit-locking step stop (MA-crossover, RSI(2))

Once a position is in profit, its stop moves up in 10% steps and never
comes back down. Bought 100 shares at $10 ($1,000):

| Gain reaches | Stop moves to | Locked-in value |
|---|---|---|
| (entry) | −10% ($9.00) | $900 |
| +11% ($11.10) | +10% ($11.00) | $1,100 |
| +21% ($12.10) | +20% ($12.00) | $1,200 |
| +31% … | +30% … | … |

The stop stays at −10% until the gain first reaches +11% (the 1% buffer
keeps the new stop from sitting right at the current price).

- **Stocks (MA-crossover, RSI(2)):** `run_step_stop_bot` checks every ~60s
  from 9:30am to 4:00pm (`StepStopTradingBot`, starts 9:25am) and raises
  the stop order at IBKR, so an intraday spike that fades still locks in
  its step. Uses client ID 1 (the ID that placed the stops) — don't run
  the MA/RSI(2) bots by hand while it's running.
- **Not on ORB:** the ORB exit comparison showed the step stop removes its
  edge (profit factor 0.98 vs 1.34 for VWAP-only), so ORB exits on VWAP
  instead. Available via `OPTIONS_STEP_STOP_ENABLED`, off.
- Not applied to Trend Join (its own breakeven + swing-low trail) or the
  iron condor (short premium; already closes at 50% of max profit).
- Settings: `STEP_STOP_ENABLED`, `STEP_STOP_PCT`, `STEP_STOP_BUFFER_PCT`.

### 1. MA-crossover + consolidation retest

Code: [strategy/ma_crossover.py](src/tradingbot/strategy/ma_crossover.py),
[strategy/consolidation_retest.py](src/tradingbot/strategy/consolidation_retest.py)

- **Entry A — golden cross:** 20-SMA crosses above 50-SMA, **and** price
  above 200-SMA, 5-day avg volume ≥ 20-day avg, RSI(14) < 70, price above
  the 20-day rolling VWAP.
- **Entry B — consolidation retest:** 15-day range ≤ 6% → breakout close
  above it on ≥ 1.2× volume → within 5 bars, a dip back to the breakout
  level that closes back above it.
- **Exit:** death cross (20-SMA below 50-SMA), or RSI(14) goes above 80 and
  falls back below it; plus the broker-side stop (−10%, then raised in 10%
  profit steps).

### 2. RSI(2) mean-reversion

Code: [strategy/rsi2_mean_reversion.py](src/tradingbot/strategy/rsi2_mean_reversion.py),
[run_rsi2_bot.py](src/tradingbot/run_rsi2_bot.py)

- **Entry:** price above 200-SMA **and** RSI(2) < 5. Blocked if today's
  volume is > 3× the 20-day average (news-driven move, not a technical dip).
- **Exit:** RSI(2) > 65; plus the broker-side stop (−10%, then raised in
  10% profit steps).
- **Universe:** fixed `RSI2_WATCHLIST` + IBKR top gainers/losers/most-active
  (price ≥ $10, volume ≥ 500k) + an S&P 500 screen via yfinance (all ~500
  names in one download; up to 20 RSI(2) buy hits, most oversold first).
  IBKR re-checks every candidate on its own bars before ordering. Both
  scans are skipped on Fridays to avoid weekend-gap risk. Runs after the
  close, so orders fill at the next open.
- **Entry order:** exits first, then buys lowest RSI(2) first, so limited
  slots (max 5) go to the most oversold setups.

**Sizing for #1 and #2:** 1% account risk per trade, capped at $2,000
notional per position (`MAX_POSITION_VALUE`), 10% broker-side **GTC** stop
(cancelled automatically when the bot sells), max 5 positions each.

### 3. Opening Range Breakout (SPY + IWM 0DTE options)

Code: [options/orb_strategy.py](src/tradingbot/options/orb_strategy.py),
[run_orb_bot.py](src/tradingbot/run_orb_bot.py)

- **Symbols:** `ORB_SYMBOLS` (SPY, IWM), traded independently — each has
  its own range, signal and position, and both can be open at once (~1%
  of equity at risk per open position). QQQ is out: the account lacks the
  Nasdaq market data subscription its quotes need.
- **Opening range:** high/low of the first 15 minutes.
- **Entry:** first *completed* 5-min bar closing above the range → buy
  ATM call; below → buy ATM put. At most one call and one put per symbol
  per day; the bot keeps watching all day (a VWAP exit on the call doesn't
  stop a later put). Each bar is judged once, right after it closes — the
  same rule as the backtest (fixed 2026-09-28: live had been judging the
  still-forming bar and missed most breakouts).
- **Exit (since 2026-09-28):** a completed 5-min bar closes back across
  today's VWAP (call: below, put: above), or the 3:55pm flatten. Max loss
  per trade is the premium paid.
- **Why VWAP-only:** [scripts/orb_exit_comparison.py](scripts/orb_exit_comparison.py)
  tested exits on 999 SPY sessions (Oct 2022–Sep 2026, with costs).
  Profit factor: VWAP-only 1.34, old exits (−30% stop, +50% target, range
  re-entry) 0.87, old exits + step stop 0.98. It wins only ~25% of trades —
  a few large winners pay for many small losses, so judge it over 40–60
  trades, not a week. The old exits are still in settings, switched off.
- One position per symbol at a time, max 10 contracts; exits checked
  every ~60s. Open positions are recovered from the journal if the
  process restarts.

### 4. Iron condor (SPY)

Code: [options/iron_condor.py](src/tradingbot/options/iron_condor.py),
[run_iron_condor_bot.py](src/tradingbot/run_iron_condor_bot.py)

- **Entry:** sell ~16-delta call and put, buy $5-wide wings, ~45 DTE. At
  most one new condor every 7 days, max 3 open.
- **Exit (first hit):** 50% of credit captured, loss = 2× credit, or 21 DTE.
- Max 10 contracts per trade. SPY only (`CREDIT_SPREAD_SYMBOLS`). QQQ is
  blocked (no Nasdaq market data subscription). IWM option quotes were
  confirmed live on 2026-09-28, but IWM condors haven't been re-tested
  since earlier 4-leg orders failed to fill.

### 5. Trend Join Long (S&P 500 intraday gap-and-go)

Code: [strategy/trend_join.py](src/tradingbot/strategy/trend_join.py),
[run_trend_join_bot.py](src/tradingbot/run_trend_join_bot.py). Source:
[Humbled Trader — AI trading bot with Claude + IBKR](https://www.humbledtrader.com/blog/ai-trading-bot-claude-ibkr/)

- **Prefilter** (yfinance, at 9:55, 10:25 … 12:55): S&P 500 names whose
  open gapped ≥ 3% above yesterday's close **and** whose close yesterday
  was above the 200-SMA; top 20 by gap size.
- **Entry** (every completed 5-min bar, 10:05–3:30) — all must pass:
  - price above yesterday's high
  - price above today's premarket high (4:00–9:30)
  - bar closes at a new high of day
  - relative volume ≥ 2× (cumulative volume vs. the 14-day average at the
    same time of day)
- **Exit:**
  - initial stop = low of day − 1% (never more than 10% below entry),
    placed at IBKR together with the entry
  - +0.75R → sell ⅓ (R = entry − initial stop)
  - +1R → stop to breakeven
  - after breakeven → trail stop 1¢ under 5-min swing lows (2 bars each side)
  - 3:51pm → flatten everything
- **Limits:** $2,000 per position, max 5 open, max 5 trades/day, one entry
  per symbol per day. Never trades symbols on the other watchlists or
  already held.
- No backtest — the article's own backtest doesn't transfer to live
  execution; paper-trade it for at least 2 weeks before judging it.

### How the strategies stay out of each other's way

- MA-crossover and RSI(2) watchlists don't overlap; RSI(2)'s scanner
  excludes the MA watchlist. Both use client ID 1, so they run one after
  the other in the same 4:30pm task (`run_rsi2_scheduled.ps1`), never at
  the same time.
- **Manual trades are never touched.** MA-crossover and RSI(2) record
  every position they open (`stock_ownership` table) and only manage
  those. Any other held stock — bought manually or by another strategy —
  is never sold by a bot, and no bot buys more of it (IBKR merges shares
  into one position). See [ownership.py](src/tradingbot/ownership.py).
- Trend Join skips both watchlists and any symbol already held, and
  records its positions in the journal (`trend_join_positions`) so RSI(2)
  never treats them as its own.
- Each long-running process has its own IBKR client ID (orders can only be
  modified by the client ID that placed them):

  | Client ID | Used by |
  |---|---|
  | 1 | MA-crossover, RSI(2) (4:30pm), step-stop manager (9:30am–4:00pm) |
  | 96 | Iron condor |
  | 97 | Trend Join |
  | 98 | ORB |
  | 99 | Snapshot reports |

## Reports

`run_snapshot` writes HTML to [reports/](reports/) and commits it locally
(no push), in a year/month/day tree:

```
reports/2026/09/28/snapshot_2026-09-28_premarket.html   (daily, per day)
reports/2026/09/weekly_2026-09-25.html                  (weekly/monthly, per month)
```

- **Daily** — 9:00am premarket and 4:30pm postmarket: open orders, open
  positions with unrealized P&L, positions closed today, account P&L,
  total balance, YTD P&L (vs. `ACCOUNT_STARTING_BALANCE`), and a
  **stop-loss coverage** table (each stock's owner and working stop, with
  an UNPROTECTED warning for any bot position missing one, plus a warning
  for any stop order left on a position that no longer exists).
- **Weekly** — Fridays after the close (Mon–Fri).
- **Monthly** — last trading day of the month after the close.

## Tools

- **S&P 500 scanner** (read-only, yfinance): top gainers/losers, unusual
  volume, and which names strategies #1 and #2 would buy right now.
  `python -m tradingbot.sp500_scanner [--top 20] [--save]` — `--save`
  writes the full scan to `scans/YYYY/MM/DD/sp500_scan_<date>.csv`, a
  separate tree from `reports/` (gitignored; local only).
- **IBKR top movers check** (read-only): `python -m scripts.check_top_movers`
- **Backtests:** `scripts/backtest_report.py`, `rsi2_backtest_report.py`,
  `options_backtest_report.py`, `iron_condor_backtest_report.py`
- **ORB exit comparison:** `python scripts/orb_exit_comparison.py --symbol SPY
  [--slippage 0.05]` — six exit-rule variants on 4 years of cached 5-min
  bars, with costs and a half-year breakdown.

## Setup

### 1. Python environment

Requires Python 3.11+. From `trading-bot/`:

```
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

### 2. Configure

```
copy .env.example .env
```

Edit `.env`: confirm `IBKR_PORT` matches your TWS/Gateway paper port (7497
for TWS paper, 4002 for Gateway paper). Every strategy parameter above is
configurable there — see `.env.example` for the full list. Never commit
`.env` — it's gitignored.

### 3. IBKR paper account

1. Log into a paper trading account in TWS or IB Gateway.
2. Enable API access: File → Global Configuration → API → Settings →
   check "Enable ActiveX and Socket Clients", confirm the port matches
   `.env`, and add `127.0.0.1` to trusted IPs.
3. Leave TWS/Gateway running whenever the bots run — they connect locally.

### 4. Scheduling

Each scheduled bot has a wrapper in [scripts/](scripts/)
(`run_*_scheduled.ps1`) registered in Windows Task Scheduler; logs go to
`logs/<bot>_<date>.log`.

## Safety

- Every bot refuses to trade on an account whose ID doesn't start with
  `DU` (IBKR paper) unless `LIVE_TRADING=true` is set in `.env`.
- Every signal, order, and position is logged to `data/journal.db` (SQLite).
- Hard caps: $2,000 per stock position, 10 contracts per options trade.
- Bots never manage, sell or add to manual positions (see ownership above).
- **Stop audit:** every MA/RSI(2) run first checks that each position it
  owns has a working stop covering the full quantity. Missing → a GTC
  stop is placed at 10% below cost; already below that level → sold. Each
  snapshot also has a "Stop-loss coverage" table that flags any bot
  position as UNPROTECTED. See [protection.py](src/tradingbot/protection.py).
- **No stops on flat positions:** a stop left behind after its shares are
  sold would open a short if triggered. The stop audit skips any stock
  with an exit order already working, and leftover stops are cancelled
  automatically (every step-stop tick and every MA/RSI(2) run). Stops on
  new entries still waiting for the open are left alone.
- A daily-loss kill-switch (`MAX_DAILY_LOSS_PCT`) halts new stock entries
  once tripped.

Run tests with `pytest`.

## Repositories

Both private: `rshukla29/trading-bot` (origin — all commits, including
the automatic daily report commits when pushed) and
`rshukla29/trading-bot-first` (copy — updated only on request).
