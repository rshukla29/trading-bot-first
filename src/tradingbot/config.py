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

    # Safety: paper accounts on IBKR have account IDs starting with "DU".
    # Trading on any other account requires this to be explicitly true.
    live_trading: bool = False

    watchlist: str = "AAPL,MSFT,AMZN,GOOGL,META,NVDA,JPM,V,UNH,HD"

    db_path: str = "data/journal.db"

    @property
    def symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.watchlist.split(",") if s.strip()]


settings = Settings()
