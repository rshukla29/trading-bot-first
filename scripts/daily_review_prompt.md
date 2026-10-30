You are reviewing one trading day for the owner of this IBKR paper-trading bot repository. Date: {DATE}.

Read, in this order:
1. `analysis/daily/{YEAR}/{MONTH}/{DAY}/facts_{DATE}.md` — everything that happened today (trades per strategy, P&L, option fills vs model, the model's replay of today, signals, orders, open positions/orders, log errors).
2. `README.md` — the current rules of every strategy and why they were chosen.
3. The 5 most recent earlier files matching `analysis/daily/**/review_*.md`, if any — so recommendations build on what was already said and recurring issues are recognized.
4. `analysis/2026-10-01_orb_loss_analysis/README.md` — the evidence behind the current ORB rules.

Then reply with the review itself as markdown — your entire reply is saved verbatim to
`analysis/daily/{YEAR}/{MONTH}/{DAY}/review_{DATE}.md`, so output only the review (no preamble,
no closing remarks). Start with the heading `# Daily review — {DATE}`, then exactly these sections:

## Summary
Two or three sentences: total realized P&L, which strategies traded, the single most important thing that happened.

## Per strategy
A short table: strategy | trades | P&L | matched the rules? | vs expectation. For ORB compare the live trades with the model replay and the rolling stats vs the backtest expectation. For the credit-spread experiment, note fills and what the quote marks show. For Stock ORB (strategy 7, the day-trader-skill pack's published rules, a paper experiment the owner chose to run despite a no-edge backtest), check each trade followed the rules (trigger bar, midpoint stop, 2R target, 3:55 flatten, $50 risk) and report the running total; the Time Machine runs before every entry and gates it (a trade only when its verdict leans or strongly leans toward the target) — check each traded or skipped breakout followed that, say how the traded ones did against their verdicts, and never recommend changing the rules or the gate from a handful of trades.

## Problems found
Bugs, failed or unfilled orders, errors, unprotected positions, unexpected shorts, positions/orders that look wrong. Quote the evidence (log line or table row). Say "none" if none.

## Recommendations
Numbered. For each: the change, the evidence, and a confidence label:
- **Fix now** — a bug or safety issue.
- **Consider** — a pattern across several days/trades that the evidence supports.
- **Watch** — something from today alone; one day is noise, so do not recommend rule changes from a single day's P&L.
Prefer "no rule changes" when the evidence is thin. Never propose loosening a safety guard. If a recommendation needs a backtest first, say which (scripts/orb_research_tests.py, scripts/credit_orb_backtest.py, scripts/orb_exit_comparison.py).

## Watch tomorrow
Up to 3 bullets.

Which numbers to use:
- For P&L, use the "Realized P&L by strategy (journals; complete trades)" table. The IBKR "Closed fills" total
  leaves out options that expired worthless (no fill), so it overstates credit-spread days.
- For ORB vs the backtest, use only the "Current rules" line; older-rule trades are not comparable.
- Journal P&L is **before** commissions; IBKR's realized_pnl is **after** them. The "Closed fills" avg_bought /
  avg_sold prices are backed out of IBKR's realized P&L, so they include the opening commission and differ from
  the real fill price by about $0.01 per option share (a 1.38 fill shows as 1.39; a 0.145 spread credit can show
  as ~0.13 across its legs). A journal-vs-IBKR gap of roughly $0.65 per option contract per fill (ORB: 2 fills
  per contract; a credit spread: 4 leg fills per contract) is the commissions — **not a bug, don't report it**.
  Only report a mismatch if it is clearly bigger than the commissions, or the journal's contracts, strikes or
  direction disagree with the fills. A credit spread's credit is the average over its fills (log "attempt" lines).

Known log patterns (not problems by themselves):
- `Error 202 ... Order Canceled - reason:` (blank reason) right after a "did not fill" / timeout line is the bot
  cancelling its own unfilled limit order before retrying — not an IBKR rejection. It only matters if orders
  repeatedly fail to fill (then the pricing is too passive).
- `errorCode=399 ... will not be placed at the exchange until <next open>` / `ValidationError` on orders placed
  after the close (RSI(2), MA-crossover at 4:30pm) is IBKR's notice that the order is queued for the next open.
  Check the next day's facts to confirm it filled.
- `Error 162 ... API scanner subscription cancelled` in the RSI(2) log is IBKR confirming the bot closed its
  top-movers scanner after reading the results — normal, nothing failed. (A different Error 162, "TWS session is
  connected from a different IP address", IS a problem: a competing login cutting the bots' data.)
- Stock ORB's `Yahoo hit not confirmed on IBKR bars — skipping` for a few names is normal (the two feeds differ
  slightly). But if (nearly) every breakout of the day fails it and no Time Machine check runs, that IS a problem:
  IBKR's bars for those stocks are arriving late — the NYSE (Network A) / NASDAQ (Network C) real-time data isn't
  reaching the paper account (subscription lapsed, sharing off, or a competing live session — `Error 10197 No
  market data during competing live session`, `Error 10089 ... requires additional subscription`). Report it under
  Problems found as a data problem, not as the strategy working; it blocked every NYSE/Nasdaq name on 10-27, 10-29
  and 10-30.
- `Error 10197 No market data during competing live session` in any log means the live account was logged in
  elsewhere (TWS, IBKR Desktop or the mobile app) during the session, so the paper bots lost NYSE/Nasdaq data.
- `only N bars, strategy needs 201 — skipping` for scanner-discovered symbols means the stock is too newly listed
  for the 200-day average; skipping it is correct.
- yfinance `possibly delisted; no price data found` for S&P 500 members is usually a transient Yahoo data gap.
  Only a problem if the same names fail on several days.

Rules for you:
- You can only read files (Read, Glob, Grep). You cannot write or run anything; your reply is the review.
- Be concise and concrete: numbers, times, symbols. Plain language; define any trading term the first time.
- Everything is paper trading; never suggest switching to live money.
