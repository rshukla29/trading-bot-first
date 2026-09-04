# trading-bot

Automated swing-trading bot for stocks: moving-average crossover entries,
20%-stop-loss risk management, one shared strategy driving two brokers —
Interactive Brokers (via `ib_async`, direct Python bot) and Robinhood (via
its official Agentic Trading MCP, driven by a scheduled Claude Code
routine). **Paper/sandbox trading only until you explicitly opt into live
money.**

## Setup

### 1. Python environment

Requires Python 3.11+ (already installed on this machine as of project
creation). From `trading-bot/`:

```
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

### 2. Configure

```
copy .env.example .env
```

Edit `.env`: at minimum confirm `IBKR_PORT` matches your TWS/Gateway paper
port (7497 for TWS paper, 4002 for Gateway paper), and adjust `WATCHLIST`,
`RISK_PCT_PER_TRADE`, `STOP_LOSS_PCT`, `MAX_POSITIONS` as you like. Never
commit `.env` — it's gitignored.

### 3. IBKR paper account

1. Log into a paper trading account in TWS or IB Gateway.
2. Enable API access: File → Global Configuration → API → Settings →
   check "Enable ActiveX and Socket Clients", confirm the port matches
   `.env`, and add `127.0.0.1` to trusted IPs.
3. Leave TWS/Gateway running whenever you run this bot — it connects to it
   locally, there is no cloud IBKR endpoint here.

### 4. Robinhood Agentic Trading (optional, for the second broker)

See [robinhood/AGENT_INSTRUCTIONS.md](robinhood/AGENT_INSTRUCTIONS.md) —
this requires you to connect Robinhood's MCP server and open a dedicated
Agentic account yourself (desktop setup, can't be automated). Once done,
tell Claude and it'll set up the scheduled routine that mirrors signals
into Robinhood.

## Usage

Run tests:

```
pytest
```

Backtest the strategy over the configured watchlist:

```
python scripts\backtest_report.py
```

Print today's signals without trading (sanity check):

```
python -m tradingbot.signal_report
```

Run the IBKR bot for real (paper account) — places orders:

```
python -m tradingbot.run_ibkr_bot
```

Schedule this daily via Windows Task Scheduler once you're comfortable
with backtest results and a few manual dry runs.

## Safety

- `run_ibkr_bot.py` refuses to trade on any account whose ID doesn't start
  with `DU` (IBKR's paper-account prefix) unless `LIVE_TRADING=true` is set
  explicitly in `.env`.
- Every signal, order, and fill is logged to `data/journal.db` (SQLite).
- A daily-loss kill-switch (`MAX_DAILY_LOSS_PCT`) halts new entries once
  tripped.
