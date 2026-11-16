# Changelog

What's new in each version of the trading bots. The newest version's notes
appear on the dashboard's Bot controls tab when an update is available.

Owner: add a "- " line under Unreleased for every change people should know
about, then publish with `scripts\release.py X.Y.Z` (see README, "Releases
and updates"). Versions: X.Y.Z, where Z is a fix, Y a new feature, X a change
that needs people to do something (e.g. new settings in .env).

## Unreleased

- First numbered release. From now on the dashboard tells you when a new version is out and installs it with one click (Update now), while the bots are idle.
- Bot controls tab: Start, Pause or Stop each strategy, or all at once; Stop can keep or close the open positions.
- The dashboard opens by itself at 8am on weekdays and updates the tab that's already open; double-click Trading dashboard to use the buttons at any hour.
- Bot start times follow daylight-saving changes automatically, and the dashboard warns if one is ever off.
- A new paper account starts with its own trade diary and reports (DB_PATH, REPORTS_DIR, ACCOUNT_START_DATE in .env).
- ORB keeps working when live option quotes are missing, using recent price bars.
