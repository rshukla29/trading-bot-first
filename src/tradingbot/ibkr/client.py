from __future__ import annotations

import asyncio
import logging
import time

from ib_async import IB, AccountValue

from tradingbot.config import Settings

logger = logging.getLogger(__name__)


class LiveAccountBlockedError(RuntimeError):
    """Raised when connected to a non-paper account without live_trading enabled."""


RETRY_SECONDS = 30.0


def connect_with_retry(ib: IB, host: str, port: int, client_id: int, wait_minutes: float = 0,
                       timeout: float = 10.0, sleep=time.sleep, clock=time.monotonic, **kw) -> None:
    """ib.connect, retried every RETRY_SECONDS for up to wait_minutes while
    TWS refuses the connection or doesn't answer. Raises the last error."""
    deadline = clock() + wait_minutes * 60
    attempt = 0
    while True:
        attempt += 1
        try:
            ib.connect(host, port, clientId=client_id, timeout=timeout, **kw)
            if attempt > 1:
                logger.info("TWS is up: connected on attempt %d", attempt)
            return
        except (ConnectionError, OSError, TimeoutError, asyncio.TimeoutError) as exc:
            left = deadline - clock()
            if left <= 0:
                raise
            logger.warning("TWS isn't accepting connections on %s:%s yet (%s); retrying for up to %.0f more minutes",
                           host, port, type(exc).__name__, left / 60)
            try:
                ib.disconnect()
            except Exception:
                pass
            sleep(min(RETRY_SECONDS, left))


class IBKRClient:
    """Thin wrapper around ib_async.IB with a paper-account safety guard."""

    def __init__(self, settings: Settings, client_id: int | None = None, wait_minutes: float = 0):
        """wait_minutes: if TWS isn't accepting connections yet (closed, at its
        log-in screen, restarting), keep retrying every 30s for this long
        instead of failing straight away. The 8am jobs use it: on Mondays TWS
        often needs its weekly log-in right around then."""
        self.settings = settings
        self.client_id = client_id if client_id is not None else settings.ibkr_client_id
        self.wait_minutes = wait_minutes
        self.ib = IB()

    def connect(self) -> None:
        connect_with_retry(self.ib, self.settings.ibkr_host, self.settings.ibkr_port, self.client_id, self.wait_minutes)
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
