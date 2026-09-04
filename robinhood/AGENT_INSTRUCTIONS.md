# Robinhood Agentic Trading — routine instructions

This is the spec for a Claude Code **scheduled routine** (set up via the
`schedule` skill once the MCP connector below is authorized) that mirrors
the IBKR bot's stock signals into Robinhood's Agentic account. It is not
executed automatically by anything in this repo — it's a prompt/runbook
for a scheduled Claude Code agent to follow.

## Prerequisite (user must do this — not automatable from here)

1. On a desktop, connect Robinhood's MCP server: `https://agent.robinhood.com/mcp/trading`
   (Claude Code: add as an MCP connector; other supported clients: Claude
   Desktop, ChatGPT, Cursor, Grok have their own connector UIs).
2. Complete Robinhood's MCP-triggered onboarding to open a dedicated
   **Agentic account** (separate from your main brokerage account — this is
   the only account the agent can place trades in).
3. Decide approval mode: per-trade approval (safer, recommended while this
   is new) vs fully autonomous. This can be changed later in the Robinhood
   app.
4. Fund the Agentic account with only the capital you want this bot risking.
5. Tell Claude once this is connected so the scheduled routine can be
   created and the MCP trading tools become callable.

## What the scheduled routine should do each run (once connected)

1. Run `python -m tradingbot.signal_report` from the `trading-bot` project
   (this reuses the exact same `MovingAverageCrossoverStrategy` signal logic
   the IBKR bot uses — no separate Robinhood-specific strategy code) and
   parse the JSON list of non-HOLD signals it prints.
2. For each `BUY` signal not already held in the Agentic account:
   - Look up current Agentic account equity via the Robinhood MCP tools.
   - Compute share quantity with the same formula as `tradingbot.risk.position_size`:
     `shares = floor(equity * RISK_PCT_PER_TRADE / (price * STOP_LOSS_PCT))`
     (defaults: 1% risk per trade, 20% stop-loss — read the actual values
     from `trading-bot/.env` so both brokers stay in sync).
   - Place the buy via the Robinhood MCP trading tool. Robinhood's MCP does
     not (as of the May 2026 beta) support attaching a broker-side stop
     order the way IBKR's bracket order does — so the stop-loss must be
     enforced by this routine itself: **on every run, check each open
     Agentic position's current price against `entry_price * (1 - STOP_LOSS_PCT)`
     and sell immediately if breached**, in addition to reacting to `EXIT`
     signals from step 1.
3. For each `EXIT` signal on a symbol currently held in the Agentic account,
   sell the full position via the Robinhood MCP trading tool.
4. Log every signal, order, and fill into the same journal the IBKR bot
   uses (`trading-bot/data/journal.db`, table schema in
   `src/tradingbot/journal.py`) with `broker="Robinhood"`, so both brokers'
   activity is visible in one place. Call `Journal.log_signal` /
   `log_order` / `log_fill` directly (Python one-liners) or extend
   `signal_report.py` with a `--log-broker` flag if this becomes a frequent
   pattern.
5. Respect the same `MAX_POSITIONS` and a daily-loss kill-switch mirroring
   `tradingbot.risk.DailyLossKillSwitch` — track realized P&L for the day
   from fills logged in step 4 and stop opening new positions once the
   `MAX_DAILY_LOSS_PCT` threshold is hit.

## Safety notes

- Stocks only for now — Robinhood's Agentic Trading beta supports equities
  and crypto; options support is coming in a later release, matching this
  project's stocks-only v1 scope.
- Because Robinhood doesn't attach a broker-side stop order via MCP yet,
  the stop-loss is only as reliable as this routine's run frequency — a
  gap-down between runs can blow through the 20% stop before the routine
  next checks. Consider running this routine at least once during market
  hours in addition to end-of-day, once you're comfortable with it.
- Start in per-trade-approval mode and watch a few cycles before switching
  to fully autonomous.
