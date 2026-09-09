"""IBKR market scanner: today's top US movers, for expanding a strategy's
symbol universe beyond a fixed watchlist. Read-only (reqScannerData), no
orders placed here.
"""
from __future__ import annotations

from ib_async import IB, ScannerSubscription

# Without price/volume filters these return mostly illiquid penny stocks
# and micro-caps (confirmed by manual check) — abovePrice/aboveVolume cut
# those out. Top %-gain/lose surfaces sharp one-day moves (relevant to a
# mean-reversion strategy); MOST_ACTIVE adds liquid large-caps that may not
# have moved enough to appear in the gain/lose scans.
SCAN_CODES = ("TOP_PERC_GAIN", "TOP_PERC_LOSE", "MOST_ACTIVE")


def fetch_top_movers(
    ib: IB,
    rows_per_scan: int = 15,
    above_price: float = 10.0,
    above_volume: int = 500_000,
) -> list[str]:
    """Return the deduplicated union of symbols across SCAN_CODES, each
    scan capped at rows_per_scan and filtered to a minimum price/volume to
    exclude penny stocks and illiquid names."""
    symbols: set[str] = set()
    for scan_code in SCAN_CODES:
        sub = ScannerSubscription(
            numberOfRows=rows_per_scan,
            instrument="STK",
            locationCode="STK.US.MAJOR",
            scanCode=scan_code,
            abovePrice=above_price,
            aboveVolume=above_volume,
        )
        data = ib.reqScannerData(sub)
        symbols.update(d.contractDetails.contract.symbol for d in data)
    return sorted(symbols)


def merge_scan_universe(fixed: list[str], movers: list[str], exclude: list[str]) -> list[str]:
    """Combine a fixed watchlist with scanner-discovered movers, dropping
    anything in `exclude` (e.g. another strategy's watchlist, to keep the
    two stock strategies from ever fighting over the same symbol)."""
    exclude_set = set(exclude)
    return sorted((set(fixed) | set(movers)) - exclude_set)
