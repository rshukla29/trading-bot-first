from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # IBKR connection
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497
    ibkr_client_id: int = 1

    # Risk management
    risk_pct_per_trade: float = 0.01
    stop_loss_pct: float = 0.10
    max_positions: int = 5
    max_daily_loss_pct: float = 0.03

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
    options_symbol: str = "SPY"
    options_bar_minutes: int = 5
    options_opening_range_minutes: int = 15
    options_risk_pct_per_trade: float = 0.01
    options_stop_loss_pct_of_premium: float = 0.30
    options_profit_target_pct: float = 0.50  # set to 0 to disable
    options_dte_days: int = 0
    options_otm_pct: float = 0.0
    options_strike_increment: float = 1.0
    options_vol_window: int = 20
    options_iv_premium_multiplier: float = 1.2
    options_exit_on_range_reentry: bool = True
    options_rate: float = 0.04

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
    credit_spread_vol_window: int = 20
    credit_spread_iv_premium_multiplier: float = 1.2
    credit_spread_rate: float = 0.04
    credit_spread_min_vol_percentile: float = 0.0  # 0 disables the vol-timing filter
    credit_spread_vol_percentile_lookback: int = 252

    # Safety: paper accounts on IBKR have account IDs starting with "DU".
    # Trading on any other account requires this to be explicitly true.
    live_trading: bool = False

    watchlist: str = "AAPL,MSFT,AMZN,GOOGL,META,NVDA,JPM,V,UNH,HD"

    db_path: str = "data/journal.db"

    @property
    def symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.watchlist.split(",") if s.strip()]


settings = Settings()
