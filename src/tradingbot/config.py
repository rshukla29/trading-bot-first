from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # IBKR connection
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497
    ibkr_client_id: int = 1

    # Risk management
    risk_pct_per_trade: float = 0.01
    stop_loss_pct: float = 0.20
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

    # Safety: paper accounts on IBKR have account IDs starting with "DU".
    # Trading on any other account requires this to be explicitly true.
    live_trading: bool = False

    watchlist: str = "AAPL,MSFT,AMZN,GOOGL,META,NVDA,JPM,V,UNH,HD"

    db_path: str = "data/journal.db"

    @property
    def symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.watchlist.split(",") if s.strip()]


settings = Settings()
