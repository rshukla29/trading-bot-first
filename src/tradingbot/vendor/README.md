# Vendored code

## timemachine.py — Time Machine (trader-timemachine)

- **Source:** [Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill),
  `trader-timemachine/timemachine.py` (main branch, read 2026-10-28).
- **License:** MIT, Copyright (c) 2026 Jake Schincariol — see
  [LICENSE-day-trader-skill](LICENSE-day-trader-skill).
- **Kept verbatim** (SHA-256 `5e73d566c78e5e42142371319491eef6257b6fc31fab5a87aa62506d03e9b5a1`),
  so its numbers match the original bit for bit. Don't edit it here; adapt it
  in [strategy/stock_orb.py](../strategy/stock_orb.py) instead. Its own docs:
  [timemachine_SKILL.md](timemachine_SKILL.md). Its tests:
  [tests/test_timemachine.py](../../../tests/test_timemachine.py) (verbatim, with
  the TSLA fixture in `tests/fixtures/trader-timemachine/`).
- **Used by:** Stock ORB (strategy #7), run before each entry as a gate: a trade only when its verdict leans toward the target.
- **By hand** (standard library only, one Yahoo request; never places an order):

  ```
  python -m tradingbot.vendor.timemachine run --symbol TSLA --date 2026-10-19 \
      --entry 364.18 --stop 358.32 --target 370.04 --entry-time "10:15 AM" --until 15:55
  python -m tradingbot.vendor.timemachine chart --symbol TSLA ...   # ASCII fan chart
  ```

- **To update:** download the new `timemachine.py` (and its tests), replace
  both files, update the SHA-256 above, run `pytest tests/test_timemachine.py
  tests/test_stock_orb.py`.
