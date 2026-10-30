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
| 1 | A Windows 10 or 11 PC that is switched on, and logged in, on market days from 9:00am to 5:00pm New York time | — |
| 2 | An Interactive Brokers (IBKR) account. It comes with a free paper account | Free to open; IBKR may ask you to fund the real account before you can buy market data |
| 3 | IBKR market data subscriptions (step 2) | A few dollars a month, depending on IBKR's prices |
| 4 | Four free programs: Python, Git, IBKR's Trader Workstation, and optionally Claude Code | Free (Claude Code needs a Claude plan) |
| 5 | Access to this code on GitHub (ask the owner if the repository is private) | — |

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

In PowerShell, paste these two lines one at a time (replace the web address
with the GitHub address the owner gave you):

```
cd $HOME\Documents
git clone https://github.com/OWNER/REPOSITORY.git trading-bot
```

The code is now in **Documents\trading-bot**. If Git asks you to sign in to
GitHub, do so in the window that opens.

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
     weekdays, converted to your PC's time zone.
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
3. In the same window, open **Lock and Exit** and set **Auto restart** to a
   time outside market hours, such as 11:45 PM. Click **OK**.
4. Leave TWS open. The bots can only work while it's running and logged
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

You're set. The bots start by themselves on the next weekday.

## Every trading day

**Before 9:00am New York time:**
- the PC is on and you're logged in to Windows (don't let it sleep);
- TWS is open and logged in to the **paper** account. About once a week,
  usually Sundays, TWS needs a full log-in again;
- you are **not** logged in to your **real** account in TWS, IBKR Desktop
  or the IBKR mobile app. While you are, IBKR stops sending live prices to
  the paper account and the bots skip trades.

**After 4:30pm:** open the newest report in the **reports** folder. It
shows the day's profit and loss for each strategy, every trade with its buy
and sell prices, and the open positions. With Claude Code, a written review
appears at 4:50pm in **analysis\daily\YEAR\MONTH\DAY**.

Market holidays need nothing: the bots find the market closed and stop.

### Stopping the bots

Press the Windows key, type **Task Scheduler**, open it, and click **Task
Scheduler Library**. The bots' tasks end in **Bot** (for example
`ORBTradingBot`). Right-click a task → **Disable** to stop it, **Enable**
to start it again. To stop everything for a day, it's enough to close TWS.

### Getting updates

When the owner improves the bots, open PowerShell and paste:

```
cd $HOME\Documents\trading-bot
git pull
```

Then run **scripts\install.ps1** again (step 5). Do this after the market
closes.

## Checklist

- [ ] IBKR account open; paper username and `DU…` account ID written down
- [ ] Four market data subscriptions, Non-Professional, shared with paper
- [ ] Python (with "Add to PATH"), Git and TWS installed; Claude Code optional
- [ ] Code downloaded to Documents\trading-bot
- [ ] Installer finished with all green OKs
- [ ] `.env` checked: `LIVE_TRADING=false`, your own stocks removed from the watchlists
- [ ] TWS logged in to paper, API enabled, Read-Only off, port 7497, 127.0.0.1 trusted
- [ ] First check report shows your `DU…` account

## When something goes wrong

Each bot keeps a diary in **Documents\trading-bot\logs**, one file per bot
per day (for example `orb_2026-11-02.log`). Open it with Notepad and look
for the message below.

| You see | What it means | What to do |
|---|---|---|
| `Couldn't connect` or `Error 502` | TWS isn't running, or its API isn't switched on | Start TWS and log in to paper; check step 7 |
| "not a paper account", and the bot stops | TWS is logged in with your real username | Log out and log in with the **paper** username |
| `Error 10089 ... requires additional subscription` | A market data subscription is missing or not shared yet | Step 2; sharing can take until the next day |
| `Error 10197 ... competing live session` | Your real account is logged in somewhere | Log out of the real account in TWS, IBKR Desktop and the mobile app |
| `Error 162 ... different IP address` | Another login took over the price feed | Same as above |
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
  day's reports on your PC.
- **PowerShell:** a Windows window where you type or paste commands.
- **`.env` file:** the bots' settings file. It stays on your PC and is
  never uploaded.
- **Scheduled task (Task Scheduler):** a Windows feature that starts a
  program at a set time. The installer creates one per bot.
- **Strategy:** a set of rules for when to buy and sell. The README
  describes each one.
- **Stop (stop-loss):** a standing order to sell if the price falls to a
  set level, to limit a loss.
- **Claude Code:** an AI assistant from Anthropic. Here it only reads the
  day's results and writes a review; it can't place trades.
