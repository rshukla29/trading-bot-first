from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # IBKR connection
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497
    ibkr_client_id: int = 1

    # run_snapshot.py's own client ID, distinct from ibkr_client_id. It's
    # scheduled at the same time as RSI2TradingBot (4:30pm ET) — sharing
    # clientId=1 caused a real collision (IBKR error 326 "client id already
    # in use") that silently killed the RSI2 run entirely on 2026-09-10.
    # Snapshot is read-only and never places/modifies orders, so giving it
    # a dedicated ID is risk-free (unlike changing the order-placing bots'
    # IDs, which would orphan their ability to manage already-open
    # positions placed under clientId=1).
    snapshot_client_id: int = 99

    # run_orb_bot.py's own client ID — a long-running intraday process,
    # given a dedicated ID up front rather than waiting to discover a
    # collision the way snapshot_client_id's was found.
    orb_client_id: int = 98

    # run_iron_condor_bot.py (3:45pm) — its own ID so it can't collide with
    # run_step_stop_bot.py, which holds client 1 from 9:30am to 4:00pm. The
    # condor bot never leaves resting orders (it waits for a fill or
    # cancels), so moving it off client 1 orphans nothing.
    condor_client_id: int = 96

    # Risk management
    risk_pct_per_trade: float = 0.01
    stop_loss_pct: float = 0.10
    max_positions: int = 5
    max_daily_loss_pct: float = 0.03
    # Hard ceiling on a single stock position's notional (shares * price),
    # independent of risk_pct_per_trade — keeps the RSI2 bot's expanded
    # top-movers universe from sizing a large position in a volatile
    # small/mid-cap the way it would size one in a stable blue-chip.
    max_position_value: float = 2000.0

    # Stepped profit-locking stop (risk.step_trailing_stop): at +11% gain the
    # stop moves to +10%, at +21% to +20%, etc. — never lowered. Applied to
    # MA-crossover and RSI(2) stock positions by run_step_stop_bot.py
    # (every ~60s during market hours) and to ORB option premium.
    step_stop_enabled: bool = True
    step_stop_pct: float = 0.10
    step_stop_buffer_pct: float = 0.01

    # Strategy
    fast_ma: int = 20
    slow_ma: int = 50
    trend_ma: int = 200  # long-term regime filter; entries only allowed above this SMA

    # Entry confirmation filters (set the window/period to 0 to disable one)
    volume_ma: int = 20
    volume_fast_ma: int = 5
    volume_multiplier: float = 1.0
    breakout_volume_multiplier: float = 1.2  # single-bar spike, used on breakouts only
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_exit_overbought: float = 80.0
    vwap_window: int = 20
    vwap_retest_tolerance: float = 0.015

    # Consolidation-breakout-retest entry pattern (second entry path)
    consolidation_retest_enabled: bool = True
    consolidation_window: int = 15
    consolidation_max_range_pct: float = 0.06
    consolidation_max_bars_since_breakout: int = 5
    consolidation_retest_tolerance: float = 0.02

    # Options ORB strategy (intraday, backtest-only — no live execution path
    # yet, since neither IBKR options market data nor a live options order
    # flow has been built here). Option prices are simulated via
    # Black-Scholes (see options/pricing.py) rather than read from real
    # historical options data, which this account can't access.
    options_symbol: str = "SPY"  # backtest default
    # Live ORB symbols, traded independently in one process: each has its
    # own opening range, signal and position, and positions on different
    # symbols can be open at the same time (so risk adds up: ~1% of equity
    # per open position). QQQ is excluded — this account lacks the Nasdaq
    # market data subscription its stock and option quotes need (10089/10091).
    orb_symbols: str = "SPY,IWM"
    options_bar_minutes: int = 5
    options_opening_range_minutes: int = 15
    options_risk_pct_per_trade: float = 0.01
    options_stop_loss_pct_of_premium: float = 0.30
    options_profit_target_pct: float = 0.0  # 0 disables (was 0.50)
    options_dte_days: int = 0
    options_otm_pct: float = 0.0
    options_strike_increment: float = 1.0
    options_vol_window: int = 20
    options_iv_premium_multiplier: float = 1.2
    options_exit_on_range_reentry: bool = False
    # ORB exit rules since 2026-09-28: exit only when a completed 5-min bar
    # closes back across the session VWAP (plus the 15:55 flatten). In
    # scripts/orb_exit_comparison.py over Oct 2022-Sep 2026 (999 sessions,
    # with costs), VWAP-only had profit factor 1.34 vs 0.87 for the old
    # stop/target/range exits and 0.98 with the step stop added — every
    # rule that cut winners or losers early removed the edge. Max loss per
    # trade is the premium paid (1% of equity, <= 10 contracts).
    options_vwap_exit: bool = True
    options_use_stop_loss: bool = False
    options_step_stop_enabled: bool = False
    options_rate: float = 0.04
    # Same reasoning as credit_spread_max_contracts_per_trade: a raw
    # risk-pct sizing formula can occasionally spike to a much larger
    # position than intended (confirmed twice already — BRZE stock sizing,
    # iron condor contract sizing) — cap regardless of what the formula
    # computes.
    options_max_contracts_per_trade: int = 10

    # RSI(2) mean-reversion swing strategy (stocks, daily bars) — the
    # second, independent stock strategy: mean-reversion rather than
    # trend-following. Same backtest-only status.
    rsi2_trend_ma: int = 200
    rsi2_period: int = 2
    rsi2_buy_threshold: float = 5.0
    rsi2_sell_threshold: float = 65.0
    rsi2_fib_lookback: int = 0  # 0 disables the fib retracement confirmation
    rsi2_fib_zone_low: float = 0.382
    rsi2_fib_zone_high: float = 0.618

    # Expand RSI2's entry universe daily with IBKR's top-movers scanner
    # (gainers/losers/most-active), on top of the fixed rsi2_watchlist —
    # the fixed list alone can miss a real setup that just isn't one of
    # those 10 names on a given day. Movers overlapping the MA-crossover
    # watchlist are excluded (see ibkr/scanner.py). Price/volume filters
    # keep the scan to liquid, real candidates instead of penny stocks.
    rsi2_scan_top_movers: bool = True
    rsi2_scan_rows: int = 15
    rsi2_scan_min_price: float = 10.0
    rsi2_scan_min_volume: int = 500_000
    # Also screen the whole S&P 500 with yfinance each run (one batched
    # download) and add its RSI(2) BUY hits, most oversold first. IBKR's
    # scanner only surfaces ~45 volatile movers and misses calm large-caps
    # with a clean dip. Every hit is re-checked on IBKR bars before any
    # order. Skipped on Fridays like the movers scan.
    rsi2_scan_sp500: bool = True
    rsi2_sp500_max_candidates: int = 20

    # Blocks a BUY when today's volume is an extreme multiple of normal —
    # the signature of a news/event-driven move (earnings, FDA, etc.)
    # rather than routine technical price action, which the RSI(2)
    # mean-reversion edge doesn't reliably transfer to. Checked live
    # against 2026-09-11's three scanner entries: PMTS spiked 21.5x
    # normal volume and CADL 3.36x (both would be blocked at 3.0x) — but
    # OMDA was only 1.75x, not a volume-spike signature at all. This
    # filter catches the clearest news-driven cases, not every risky
    # entry; it's a partial mitigation, not a complete fix. Applies to
    # every entry, not only scanner-discovered ones, though the calm
    # fixed watchlist rarely triggers it.
    rsi2_volume_spike_window: int = 20
    rsi2_volume_spike_multiplier: float = 3.0

    # Iron condor / credit-spread strategy on SPY, QQQ, IWM (daily bars,
    # 30-45 DTE) — the second, independent options strategy: selling
    # defined-risk premium rather than buying long options. Parameters
    # grounded in published options-selling research (16-delta shorts, 45
    # DTE, 50% profit target, 100%-of-credit stop, 21-DTE forced close).
    # Same Black-Scholes-simulation caveat as the ORB strategy.
    credit_spread_target_delta: float = 0.16
    credit_spread_wing_width: float = 5.0
    credit_spread_dte_days: int = 45
    credit_spread_profit_target_pct: float = 0.50
    credit_spread_stop_loss_multiple: float = 2.0
    credit_spread_dte_forced_close: int = 21
    credit_spread_entry_interval_days: int = 7
    credit_spread_max_concurrent: int = 3
    credit_spread_risk_pct_per_trade: float = 0.02
    # Hard ceiling regardless of what risk_pct_per_trade computes — a
    # narrow wing_width on a large account can otherwise size a 4-leg
    # combo at 50+ contracts, which is both a much bigger real-liquidity
    # ask than 1-5 contracts and a materially different risk exposure than
    # what gets tested informally before trusting a strategy live.
    credit_spread_max_contracts_per_trade: int = 10
    credit_spread_vol_window: int = 20
    credit_spread_iv_premium_multiplier: float = 1.2
    credit_spread_rate: float = 0.04
    credit_spread_min_vol_percentile: float = 0.0  # 0 disables the vol-timing filter
    credit_spread_vol_percentile_lookback: int = 252

    # "Trend Join Long" intraday gap-and-go on S&P 500 names (see
    # strategy/trend_join.py for the full rule set and source). Sizing
    # reuses risk_pct_per_trade and max_position_value (the account-wide
    # $2,000 cap — the article used $2,500 = 10% of a $25k account).
    trend_join_client_id: int = 97
    trend_join_min_gap_pct: float = 3.0
    trend_join_min_price: float = 3.0
    trend_join_trend_ma: int = 200
    trend_join_min_rvol: float = 2.0
    trend_join_rvol_lookback_days: int = 14
    trend_join_max_candidates: int = 20
    trend_join_max_positions: int = 5
    trend_join_max_trades_per_day: int = 5
    trend_join_bar_minutes: int = 5
    trend_join_lod_stop_buffer_pct: float = 0.01
    trend_join_partial_r: float = 0.75
    trend_join_partial_fraction: float = 0.3333
    trend_join_breakeven_r: float = 1.0
    trend_join_swing_bars: int = 2
    trend_join_entry_start: str = "10:05"
    trend_join_entry_end: str = "15:30"
    trend_join_flatten_time: str = "15:51"
    # Prefilter (yfinance daily-bar gap scan) reruns at these ET times.
    trend_join_prefilter_times: str = "09:55,10:25,10:55,11:25,11:55,12:25,12:55"

    # Safety: paper accounts on IBKR have account IDs starting with "DU".
    # Trading on any other account requires this to be explicitly true.
    live_trading: bool = False

    watchlist: str = "AAPL,MSFT,AMZN,GOOGL,META,NVDA,JPM,V,UNH,HD,SPCX,TSLA,PLTR,AVGO,ASTX,SOXL,SOXS,LLY,HOOD"

    # Deliberately non-overlapping with `watchlist`: running two independent
    # stock strategies live against the same symbols risks one strategy's
    # EXIT signal closing a position the other strategy actually opened,
    # since IBKR positions carry no metadata about which strategy owns
    # them. Separate symbol universes sidestep the problem entirely.
    rsi2_watchlist: str = "XOM,JNJ,PG,KO,DIS,WMT,BAC,PFE,CSCO,INTC"

    # Live iron condor symbols. SPY-only for now: QQQ and IWM options both
    # failed to fill across every live test (10091 "additional subscription
    # required" errors on QQQ; IWM fails silently) while SPY filled cleanly
    # — looks like an account-level options market data entitlement gap for
    # QQQ/IWM specifically, not something fixable in code. Add them back
    # here once that's resolved; the backtest already covers all three.
    credit_spread_symbols: str = "SPY"

    db_path: str = "data/journal.db"

    # Daily pre/post-market snapshot (run_snapshot.py): open orders,
    # per-position unrealized P&L, account realized/unrealized/daily P&L,
    # total balance, and YTD P&L. Written as HTML under reports_dir and
    # committed to git (no push) so the account history accumulates in
    # version control.
    reports_dir: str = "reports"
    # S&P 500 scanner CSVs (sp500_scanner --save), kept apart from reports
    # in the same year/month/day tree: scans/YYYY/MM/DD/. Gitignored.
    scans_dir: str = "scans"

    # IBKR's own realizedPnL/RealizedPnL fields reset to 0 every day and
    # there's no calendar-YTD tag in the API without separately configuring
    # Flex Query reports — so YTD P&L here is computed as
    # (current NetLiquidation - account_starting_balance). $1M is the
    # standard IBKR paper-account default; set this to the account's
    # actual balance at the start of the year for an accurate figure.
    account_starting_balance: float = 1_000_000.0

    @property
    def symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.watchlist.split(",") if s.strip()]

    @property
    def rsi2_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.rsi2_watchlist.split(",") if s.strip()]

    @property
    def trend_join_prefilter_time_list(self) -> list[str]:
        return [t.strip() for t in self.trend_join_prefilter_times.split(",") if t.strip()]

    @property
    def orb_symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.orb_symbols.split(",") if s.strip()]

    @property
    def credit_spread_symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.credit_spread_symbols.split(",") if s.strip()]


settings = Settings()
