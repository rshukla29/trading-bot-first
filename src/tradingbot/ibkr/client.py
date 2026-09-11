from __future__ import annotations

import logging

from ib_async import IB, AccountValue

from tradingbot.config import Settings

logger = logging.getLogger(__name__)


class LiveAccountBlockedError(RuntimeError):
    """Raised when connected to a non-paper account without live_trading enabled."""


class IBKRClient:
    """Thin wrapper around ib_async.IB with a paper-account safety guard."""

    def __init__(self, settings: Settings, client_id: int | None = None):
        self.settings = settings
        self.client_id = client_id if client_id is not None else settings.ibkr_client_id
        self.ib = IB()

    def connect(self) -> None:
        self.ib.connect(
            self.settings.ibkr_host,
            self.settings.ibkr_port,
            clientId=self.client_id,
        )
        self._enforce_paper_account_guard()
        logger.info("Connected to IBKR at %s:%s", self.settings.ibkr_host, self.settings.ibkr_port)

    def _enforce_paper_account_guard(self) -> None:
        accounts = self.ib.managedAccounts()
        if not accounts:
            return
        is_paper = all(acct.startswith("DU") for acct in accounts)
        if not is_paper and not self.settings.live_trading:
            self.ib.disconnect()
            raise LiveAccountBlockedError(
                f"Connected account(s) {accounts} do not look like paper accounts "
                "(paper account IDs start with 'DU'), but LIVE_TRADING is not set to "
                "true. Refusing to proceed. Set LIVE_TRADING=true in .env only when "
                "you intend to trade real money."
            )

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()

    def account_summary(self) -> list[AccountValue]:
        return self.ib.accountValues()

    def net_liquidation(self) -> float:
        for av in self.ib.accountValues():
            if av.tag == "NetLiquidation" and av.currency == "USD":
                return float(av.value)
        raise RuntimeError("NetLiquidation value not found in account summary")

    def positions(self):
        return self.ib.positions()

    def __enter__(self) -> "IBKRClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.disconnect()
