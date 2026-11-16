# Setup guide: run the trading bots on your own PC

This guide takes you from nothing to bots trading a **practice (paper)
account** at Interactive Brokers. No programming knowledge needed: every
step says what to click or what to copy and paste. Words in *italics* are
explained in the [glossary](#glossary) at the end.

**Time needed:** about 1–2 hours of your time. Opening a brokerage account
and getting market data approved can take a few days.

**Safety:** the bots only trade a *paper account* (fake money). They refuse
to touch a real-money account unless someone deliberately changes a
setting. Leave it that way.

## What you need

| | What | Cost |
|---|---|---|
| 1 | A Windows 10 or 11 PC that is switched on, and logged in, on market days from 8:00am to 6:00pm New York time | — |
| 2 | An Interactive Brokers (IBKR) account. It comes with a free paper account | Free to open; IBKR may ask you to fund the real account before you can buy market data |
| 3 | IBKR market data subscriptions (step 2) | A few dollars a month, depending on IBKR's prices |
| 4 | Four free programs: Python, Git, IBKR's Trader Workstation, and optionally Claude Code | Free (Claude Code needs a Claude plan) |
| 5 | The code, from the public GitHub repository **rshukla29/trading-bot-first** (no account needed) | — |

## Step 1. Open the Interactive Brokers account

1. Go to **interactivebrokers.com** and open an account. Approval can take a
   few days.
2. Once approved, log in to **Client Portal** (the website).
3. Go to **Settings → Paper Trading Account**. Write down:
   - the **paper username** (different from your normal one); reset the
     paper password there if you need to;
   - the **paper account ID**. It starts with `DU`.

From now on there are two logins. Your **real** login is for the website and
settings. Your **paper** login is the only one the bots ever use.

## Step 2. Subscribe to market data

The bots need live prices. Without them, IBKR sends stock prices about 15
minutes late, and the bots skip most trades.

1. In Client Portal (real login) go to **Settings → Market Data
   Subscriptions → Configure**.
2. If asked, answer the questionnaire as a **Non-Professional**. Getting this
   wrong makes data many times more expensive.
3. Subscribe to these four (IBKR's exact names). Choose the "level 1" or
   streaming versions, not "snapshot":

   | Subscription | Gives prices for |
   |---|---|
   | NYSE (Network A/CTA) | Stocks listed on the New York Stock Exchange |
   | NASDAQ (Network C/UTP) | Stocks listed on Nasdaq |
   | NYSE American, BATS, ARCA, IEX (Network B) | SPY and other funds |
   | OPRA (US Options Exchanges) | SPY options |

4. Go to **Settings → Paper Trading Account** and switch on **Share
   real-time market data subscriptions with paper trading account**. It
   can take until the next day to start.
5. When it has started, **log out of TWS and log back in** with the paper
   username (step 7). The paper account only picks up the shared
   subscriptions on a fresh log-in.

> The paper account gets the subscriptions of the **real username it
> belongs to**. If you ever create a second paper user, make sure it hangs
> off the real username that pays for the data, and switch sharing on for
> it too.

## Step 3. Install the programs

Install each one with its normal installer. Accept the defaults unless the
step says otherwise.

1. **Python** (runs the bots): go to **python.org → Downloads → Windows**
   and download the latest Python 3 (3.11 or newer). On the first installer
   screen, **tick "Add python.exe to PATH"** before you click Install. This
   is the step people most often miss.
2. **Git** (downloads the code and saves the daily reports): go to
   **git-scm.com → Download for Windows**. Click Next through every screen.
3. **Trader Workstation (TWS)** (IBKR's trading program; the bots connect
   to it): on interactivebrokers.com go to **Trading → Trader Workstation**
   and download the Windows version.
4. **Claude Code** *(optional)*: writes a plain-English review of each
   trading day at 4:50pm. You can skip it; everything else works without it.
   To install, open PowerShell (see the box below), paste this line and
   press Enter:

   ```
   irm https://claude.ai/install.ps1 | iex
   ```

   Then type `claude`, press Enter, and sign in with your Claude account
   once. Type `/exit` to close it.

> **How to open PowerShell:** press the Windows key, type `PowerShell`, and
> click **Windows PowerShell**. A window with a blinking cursor opens. To
> run a command from this guide, copy it, right-click inside the window to
> paste, and press Enter.

## Step 4. Download the code

In PowerShell, paste these two lines one at a time:

```
cd $HOME\Documents
git clone https://github.com/rshukla29/trading-bot-first.git trading-bot
```

The code is now in **Documents\trading-bot**. Download it this way (not as
a ZIP): it's how your copy finds out about new versions later (see
[Getting updates](#getting-updates)).

## Step 5. Run the installer

1. Open **File Explorer** and go to **Documents\trading-bot\scripts**.
2. Right-click **install.ps1** → **Run with PowerShell**. If Windows asks
   whether to allow it, choose **Open** or **Yes**.
3. A window shows each step with a green **OK**:
   - checks Python and Git (the first time, it asks your name and email:
     Git stamps them on the saved reports; they stay on your PC);
   - installs what the bots need (a few minutes);
   - creates the settings file `.env`;
   - runs the bots' self-tests (all must pass);
   - creates the scheduled tasks that start each bot at the right time on
     weekdays. The times are New York time, listed in
     **scripts\schedule.json**, and they automatically follow the clock
     when it changes for daylight saving;
   - sets up the **control server** (task `BotControlServer`) behind the
     dashboard's Start / Pause / Stop buttons. Monday to Friday at 8am it
     rebuilds the dashboard and opens it in your browser (if the dashboard is
     still open from the day before, that same tab updates instead); it
     stops at 6pm and leaves the browser open;
   - puts a **Trading dashboard** shortcut on your desktop.
4. If you see a red **PROBLEM**, the line says what to fix. Fix it and run
   the installer again. Running it twice is safe.

## Step 6. Check the settings file

1. In File Explorer, open **Documents\trading-bot**. Right-click the file
   **.env** → **Open with → Notepad**. (If you don't see it, click **View →
   Show → File name extensions / Hidden items**.)
2. Check these lines, then save (Ctrl+S):

   | Line | What it should say |
   |---|---|
   | `IBKR_PORT=7497` | Leave as 7497 (TWS paper account). |
   | `LIVE_TRADING=false` | **Must stay `false`.** This is what keeps the bots off real money. |
   | `ACCOUNT_STARTING_BALANCE=1000000.0` | Your paper account's starting balance (shown in TWS). Used for the year-to-date numbers. |
   | `DB_PATH=data/journal.db` | The bots' diary of every trade. Leave it, unless you switch to a new paper account (see [Starting over with a new paper account](#starting-over-with-a-new-paper-account)). |
   | `REPORTS_DIR=reports` | Where the reports and the dashboard are saved. Same as above. |
   | `WATCHLIST=...` and `RSI2_WATCHLIST=...` | The stocks two of the strategies buy. **Delete any stock you own or trade yourself**, so no bot buys it. The two lists must not share a stock. |

Everything else has sensible values. The README explains each strategy if
you want to change them later.

## Step 7. Set up Trader Workstation (TWS)

1. Start TWS and log in with your **paper** username (from step 1). TWS
   marks the window as a paper (simulated) account, and the account
   number shown starts with `DU`.
2. Click **File → Global Configuration → API → Settings** and set:
   - **Enable ActiveX and Socket Clients**: ticked;
   - **Read-Only API**: **not** ticked (the bots must be able to place
     orders);
   - **Socket port**: `7497`;
   - **Trusted IPs**: click Create and add `127.0.0.1`.
3. In the same window, open **Presets → Precautionary Settings** and set
   **Size limit** (and **Total value limit**, if it's low) high enough for
   the bots' orders: at least **10** (the iron condor sends 10 contracts at
   once). A new paper user starts at 5, and IBKR then rejects bigger orders
   with `Error 383`.
4. Open **Lock and Exit** and set **Auto restart** to a time outside market
   hours, such as 11:45 PM. Click **OK**.
5. Leave TWS open. The bots can only work while it's running and logged
   in to paper.

## Step 8. First check

With TWS running and logged in to paper, open PowerShell and paste:

```
cd $HOME\Documents\trading-bot
.venv\Scripts\python.exe -m tradingbot.run_snapshot premarket
```

After a few seconds a report appears in **Documents\trading-bot\reports**.
Double-click it to open it in your browser. It should show your `DU…`
account and its balance. If you get an error instead, see
[When something goes wrong](#when-something-goes-wrong).

Then, on a weekday between 8am and 6pm New York time, open
**http://127.0.0.1:8765** in your browser: the dashboard (or, before its
first build, the Bot controls page) should load and list the strategies.

You're set. The bots start by themselves on the next weekday.

## Every trading day

**Before 8:00am New York time:**
- the PC is on and you're logged in to Windows (don't let it sleep);
- TWS is open and logged in to the **paper** account. About once a week,
  usually Sundays, TWS needs a full log-in again;
- you are **not** logged in to your **real** account in TWS, IBKR Desktop
  or the IBKR mobile app. While you are, IBKR stops sending live prices to
  the paper account and the bots skip trades.

**What happens by itself:**

| Time (New York) | What you'll see |
|---|---|
| 8:00am | The premarket report is saved, and the dashboard opens in your browser on the **Bot controls** tab. If yesterday's dashboard tab is still open, it updates itself instead of opening a second one |
| 9:25am – 4:00pm | The bots trade. Check the Bot controls tab any time: each strategy shows Running / Paused / Stopped, today's profit or loss and what's open |
| 4:30pm | The postmarket report is saved and the open dashboard updates itself |
| 4:50pm | With Claude Code, a written review appears on the dashboard |
| 6:00pm | The control server stops. The dashboard stays open but its buttons are offline until 8am the next weekday (or until you double-click **Trading dashboard**) |

The dashboard shows the account value, profit and loss per day, week,
month and strategy, every trade, the open positions, links to every report,
the written reviews and whether each bot ran cleanly. To refresh it by hand,
double-click **Trading dashboard** on your desktop.

The newest report in the **reports** folder (inside the folder named in
`REPORTS_DIR`) has the full detail for the day: each strategy's profit and
loss, every trade with its buy and sell prices, and the open positions with
live prices. Written reviews are saved in **analysis\daily\YEAR\MONTH\DAY**.

Market holidays need nothing: the bots find the market closed and stop.

### Starting, pausing and stopping a strategy

The dashboard opens by itself at 8am on weekdays (or open
**http://127.0.0.1:8765** in your browser, or use the desktop shortcut) and
starts on the **Bot controls** tab. An open dashboard updates itself when
new data arrives (8am and 4:30pm). Each strategy has three buttons;
the bar at the top does the same for all of them at once.

**At the weekend, in the evening or before 8am** (for example to press
**Start all** on a Sunday so the bots trade on Monday), double-click
**Trading dashboard** on your desktop. It starts the control server, so
the dashboard opens with working buttons. Make your choice and close the
tab; the server closes itself about 30 minutes later. Nothing trades
until the bots' usual times on the next weekday.

- **Start**: trades as usual.
- **Pause**: no new trades. Anything already open is still looked after:
  stops, profit locks, targets and the 3:55pm close all keep working.
- **Stop**: no new trades, and the bot stays off on the following days. If
  the strategy has open positions you choose:
  - **keep positions**: the bot keeps looking after them until they close,
    then stops;
  - **close positions at market**: the bot sells (or buys back) its own
    positions right away. Your own manual trades are never touched.

A choice stays until you press **Start** again; it doesn't reset
overnight. The expiry guard and step stop protect open positions and
can't be switched off. The buttons only work from the dashboard on this PC
(http://127.0.0.1:8765): other computers and other websites can't use them.

Good to know:
- **During market hours** a button acts straight away. Pressing **Start**
  on a bot that had stopped starts it again; **close positions at market**
  starts whichever bot has to do the selling.
- **Outside market hours** the choice is saved and the bot follows it when
  it next runs. *Close at market* for RSI(2) and MA crossover stocks then
  happens at their 4:30pm run or the next morning.
- **When live prices aren't working** (for example on a new account, or
  while your real account is logged in somewhere), press **Pause all**
  before 9:30am and **Start all** once prices are back.
- If the Bot controls tab says it **can't reach the control server**,
  double-click **Trading dashboard** on your desktop. If that doesn't help,
  start the `BotControlServer` task in Task Scheduler (right-click → Run)
  or run **scripts\install.ps1** again.
- If it says **a bot's start time is off**, run
  **scripts\register_tasks.ps1** (right-click → Run with PowerShell).

### Changing when a bot runs

The times are in **scripts\schedule.json** (New York time). Open it with
Notepad, change a time, save, then right-click
**scripts\register_tasks.ps1** → **Run with PowerShell**. The tasks follow
your PC's clock, so daylight-saving changes need nothing. (If your PC isn't
on New York time and your clocks change on different dates than the US,
run register_tasks.ps1 again after each change; the Bot controls tab warns
you when a time is off.)

### Turning off a scheduled task

Press the Windows key, type **Task Scheduler**, open it, and click **Task
Scheduler Library**. The bots' tasks end in **Bot** (for example
`ORBTradingBot`). Right-click a task → **Disable** to stop it, **Enable**
to start it again. To stop everything for a day, it's enough to close TWS.

### Starting over with a new paper account

Your old reports, reviews and trade diary are kept; the new account gets
its own.

1. Close the old account's positions (or press **Stop all → close
   positions at market** on the Bot controls tab).
2. In Client Portal, create or reset the paper account, then log TWS in
   with the new paper username.
3. TWS keeps its settings per user, so repeat **step 7** for the new user
   (API settings and the order size limit), and switch on market data
   sharing for it (**step 2**, items 4 and 5).
4. In `.env`, point the bots at new files, using the new `DU…` number:

   ```
   DB_PATH=data/journal_DU1234567.db
   REPORTS_DIR=reports/DU1234567
   ACCOUNT_START_DATE=2026-11-16
   ```

   `ACCOUNT_START_DATE` is the new account's first trading day: the
   dashboard leaves out older logs and reviews. Change
   `ACCOUNT_STARTING_BALANCE` too if the new account doesn't start at
   $1,000,000.
5. The next 8am run creates the new (empty) report and dashboard.

### Getting updates

You don't need to watch for updates. When the owner publishes a new
version:

1. At 8am the next weekday, Windows shows a notification: **Trading bots
   update: Version X is available**. The **Bot controls** tab shows a
   blue banner with what's new.
2. After 5:15pm, at the weekend or before 8am (New York time), press
   **Update now** on that banner and confirm. It takes about a minute.
   The page reloads itself and says **Updated from … to …**.

Your settings (`.env`), your trades and your reports stay as they are. If
anything goes wrong, the update puts the previous version back, and the
banner says what happened.

The button stays greyed out while the bots might be trading (weekdays 8am
to 5:15pm). The version you have is shown at the bottom of the Bot controls
tab.

> If the banner says **files were edited on this PC**, you (or someone)
> changed one of the bots' code files. Settings belong in `.env`, which is
> never touched. Ask for help before undoing the edit.

To update by hand instead, open PowerShell and paste:

```
cd $HOME\Documents\trading-bot
powershell -ExecutionPolicy Bypass -File scripts\update.ps1
```

## Checklist

- [ ] IBKR account open; paper username and `DU…` account ID written down
- [ ] Four market data subscriptions, Non-Professional, shared with paper
- [ ] Python (with "Add to PATH"), Git and TWS installed; Claude Code optional
- [ ] Code downloaded to Documents\trading-bot with `git clone` (not a ZIP)
- [ ] Installer finished with all green OKs
- [ ] `.env` checked: `LIVE_TRADING=false`, your own stocks removed from the watchlists
- [ ] TWS logged in to paper, API enabled, Read-Only off, port 7497, 127.0.0.1 trusted
- [ ] TWS order size limit at least 10 (Presets → Precautionary Settings)
- [ ] Logged out of TWS and back in after market data sharing started
- [ ] First check report shows your `DU…` account
- [ ] http://127.0.0.1:8765 opens the dashboard on a weekday between 8am and 6pm

## When something goes wrong

Each bot keeps a diary in **Documents\trading-bot\logs**, one file per bot
per day (for example `orb_2026-11-02.log`). Open it with Notepad and look
for the message below.

| You see | What it means | What to do |
|---|---|---|
| `Couldn't connect` or `Error 502` | TWS isn't running, or its API isn't switched on | Start TWS and log in to paper; check step 7 |
| "not a paper account", and the bot stops | TWS is logged in with your real username | Log out and log in with the **paper** username |
| `Error 10089 ... requires additional subscription` | A market data subscription is missing or not shared yet | Step 2; sharing can take until the next day |
| `Error 10168`, `2186` or `354` ... not subscribed | The paper account isn't receiving the shared market data yet (common right after creating a paper account) | Step 2, items 4 and 5: sharing on for this paper user, then log TWS out and in. Press **Pause all** until it works |
| `Error 383 ... size exceeds the Size Limit` | TWS's precautionary order size limit is lower than the order | Step 7, item 3: raise the size limit to at least 10 |
| `Error 10197 ... competing live session` | Your real account is logged in somewhere | Log out of the real account in TWS, IBKR Desktop and the mobile app |
| `Error 162 ... different IP address` | Another login took over the price feed | Same as above |
| Bot controls says **a bot's start time is off** | A task's time drifted from `scripts\schedule.json` (e.g. after a daylight-saving change) | Run **scripts\register_tasks.ps1** (right-click → Run with PowerShell) or the installer again |
| The dashboard didn't open at 8am | The PC was asleep or off, or `BotControlServer` is disabled | Open http://127.0.0.1:8765; if it doesn't load, right-click the `BotControlServer` task → Run, or run the installer again |
| Bot controls says it **can't reach the control server** | The server is off (outside 8am–6pm on weekdays, or it stopped) | Double-click **Trading dashboard** on your desktop: it starts the server and reopens the dashboard. Still not working: run the `BotControlServer` task |
| **Update now** is greyed out | The bots might be trading (weekdays 8am–5:15pm New York) | Press it after 5:15pm, at the weekend or before 8am |
| The update banner says **files were edited on this PC** | One of the bots' code files was changed by hand | Settings belong in `.env`. Ask the owner for help before undoing the edit |
| The update banner says the **update failed** | Usually no internet, or a problem in the new version | You're still on the old version and nothing else changed. Try again later; if it keeps failing, send the owner the newest `logs\update_….log` |
| No update notice, though the owner released one | Your copy checks when the control server starts (8am on weekdays), or it was installed before version 1.0.0, or downloaded as a ZIP | Double-click **Trading dashboard**. Installed before 1.0.0: run `git pull` and the installer once. ZIP: download it again with `git clone` (step 4) |
| `Error 326 ... client id is already in use` | The same bot was started twice | Close the extra window; it fixes itself the next day |
| Orders are rejected as "read-only" | TWS's Read-Only API box is ticked | Untick it (step 7) |
| A log file is empty or missing | The task didn't start, or the installer didn't finish | Run **scripts\install.ps1** again |
| `Claude review failed` in the review log | Claude Code isn't installed or not signed in | Step 3, item 4, or ignore it if you don't use the review |
| The installer says Python isn't found | "Add python.exe to PATH" wasn't ticked | Run the Python installer again, choose **Modify**, tick **Add Python to environment variables**, then open a new PowerShell window |

## Glossary

- **Paper account / paper trading:** a practice account at IBKR with fake
  money. Trades work exactly like real ones, but nothing real is bought or
  sold. Its ID starts with `DU`.
- **Real (live) account:** your actual brokerage account with real money.
  The bots never use it.
- **TWS (Trader Workstation):** IBKR's trading program for your PC. The
  bots talk to IBKR through it, so it must be open.
- **API:** the doorway TWS opens so other programs (the bots) can place
  orders and read prices. Switched on in step 7.
- **Market data subscription:** a monthly fee to the stock exchanges for
  live prices. Without it prices arrive about 15 minutes late.
- **Non-Professional:** an individual trading their own money. Pays much
  lower data fees than a professional.
- **Python:** the programming language the bots are written in. You only
  install it; you never write any.
- **Git / GitHub:** Git keeps track of versions of files; GitHub is the
  website where the code is stored. The bots also use Git to save each
  day's reports on your PC, and to check for new versions.
- **Version / release:** a numbered edition of the bots, such as 1.0.0.
  The owner publishes a new one when something is improved; yours is shown
  at the bottom of the Bot controls tab.
- **PowerShell:** a Windows window where you type or paste commands.
- **`.env` file:** the bots' settings file. It stays on your PC and is
  never uploaded.
- **Scheduled task (Task Scheduler):** a Windows feature that starts a
  program at a set time. The installer creates one per bot.
- **Control server:** a small program on your PC (weekdays 8am–6pm, or
  whenever you double-click Trading dashboard) that
  shows the dashboard at http://127.0.0.1:8765 and carries out the Start /
  Pause / Stop buttons. Only this PC can use it.
- **Bot controls:** the dashboard tab with the Start / Pause / Stop buttons.
- **Strategy:** a set of rules for when to buy and sell. The README
  describes each one.
- **Stop (stop-loss):** a standing order to sell if the price falls to a
  set level, to limit a loss.
- **Claude Code:** an AI assistant from Anthropic. Here it only reads the
  day's results and writes a review; it can't place trades.
