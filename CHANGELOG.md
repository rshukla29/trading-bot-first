# Changelog

What's new in each version of the trading bots. The newest version's notes
appear on the dashboard's Bot controls tab when an update is available.

Owner: add a "- " line under Unreleased for every change people should know
about, then publish with `scripts\release.py X.Y.Z` (see README, "Releases
and updates"). Versions: X.Y.Z, where Z is a fix, Y a new feature, X a change
that needs people to do something (e.g. new settings in .env).

## Unreleased

## 1.3.2 — 2026-11-24

- Reports, daily reviews and analysis now stay on this computer and are no longer saved into the download, so a **new install starts with an empty dashboard** instead of showing older paper accounts' results. Updating removes the old reports that came with the download; reports this copy made itself are kept.

- The setup check's **SPY option prices** test no longer warns "Couldn't look up an option" when IBKR still lists last week's expired date: it tests the nearest expiry from today on.

## 1.3.1 — 2026-11-19

- **Update now** works again on a copy that has been running: the bots save each day's reports as commits, and the updater used to refuse a copy with commits of its own. A copy stuck on an older version needs one manual update first (see the release notes).

## 1.3.0 — 2026-11-19

- New paper-experiment strategy **ORB-0DTE-HP**: one SPY same-day call or put a day, bought on the first 5-minute candle before 11am that closes on the same side of the 9 EMA, 50 SMA, 200 SMA and VWAP as a fresh MACD cross (MACD above 0 for a call, below for a put), and held to 3:55pm. It has its own card on the Rules tab and its own Start / Pause / Stop buttons. In a 4-year backtest it made money every year (profit factor 1.39), but option prices there are estimated, so it starts as a paper experiment.

## 1.2.0 — 2026-11-18

- New **Rules** tab on the dashboard: type the capital the bots trade with (or leave it blank for the whole account), and set each strategy's own trade size (% of capital, $ or a fixed number of contracts or shares), max contracts, max premium, stop loss, first and step stop, profit target, trades per day and daily loss limit, in $ or %. Each card shows what the rules mean at your capital before you save, refuses unsafe values, and every change is logged. Nothing changes until you save: the starting values are your current settings.
- RSI(2) and MA crossover each have their own daily loss limit on the Rules tab ($ or % of capital, off until you set it) that really stops new entries after a bad day. The old `MAX_DAILY_LOSS_PCT` setting is no longer used (it never counted the day's losses).
- The readiness check tests the Rules tab's saved rules, and previews an order as large as the biggest option order your rules allow.
- One-click install: download **Install-Trading-Bots.cmd** from the release page and double-click it. It installs Python and Git if needed, downloads the bots and sets everything up, with no PowerShell typing (docs/SETUP.md, step 4).

## 1.1.1 — 2026-11-16

- The 8am premarket report and readiness check now wait up to 10 minutes for TWS to be logged in (on Mondays it often still needs its weekly log-in) instead of failing straight away.
- Bot health no longer shows an error when TWS refused a connection that later succeeded, or for the readiness check's own log (its results are in its list).

## 1.1.0 — 2026-11-15

- New readiness check on the Bot health tab: checks in about 20 seconds that everything the bots need is in place (TWS and the paper account, live prices for SPY, stocks and options, price history, the scheduled tasks, safe settings, the PC not sleeping, and that TWS accepts the bots' order size), with the fix for anything that's wrong. Press Run checks now any time; it never places orders.
- It runs by itself at 8am on weekdays; the Bot controls tab says "Ready to trade" or how many problems there are (top right), and Windows notifies you if something's wrong.

## 1.0.1 — 2026-11-15

- The version you're on is shown in the top right corner of the Bot controls tab, with a small Update button next to it when a new version is out.
- Fix: an empty blue box no longer appears on the Bot controls tab when there's no update.

## 1.0.0 — 2026-11-15

- First numbered release. From now on the dashboard tells you when a new version is out and installs it with one click (Update now), while the bots are idle.
- Bot controls tab: Start, Pause or Stop each strategy, or all at once; Stop can keep or close the open positions.
- The dashboard opens by itself at 8am on weekdays and updates the tab that's already open; double-click Trading dashboard to use the buttons at any hour.
- Bot start times follow daylight-saving changes automatically, and the dashboard warns if one is ever off.
- A new paper account starts with its own trade diary and reports (DB_PATH, REPORTS_DIR, ACCOUNT_START_DATE in .env).
- ORB keeps working when live option quotes are missing, using recent price bars.
