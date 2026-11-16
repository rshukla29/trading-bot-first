# Changelog

What's new in each version of the trading bots. The newest version's notes
appear on the dashboard's Bot controls tab when an update is available.

Owner: add a "- " line under Unreleased for every change people should know
about, then publish with `scripts\release.py X.Y.Z` (see README, "Releases
and updates"). Versions: X.Y.Z, where Z is a fix, Y a new feature, X a change
that needs people to do something (e.g. new settings in .env).

## Unreleased

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
