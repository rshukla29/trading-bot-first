from __future__ import annotations

import pandas as pd
from ib_async import IB, Stock, util


def fetch_daily_bars(ib: IB, symbol: str, duration: str = "2 Y") -> pd.DataFrame:
    """Fetch daily historical bars for a US stock symbol as a DataFrame indexed by date."""
    contract = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(contract)
    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,
    )
    df = util.df(bars)
    if df is None or df.empty:
        raise ValueError(f"No historical data returned for {symbol}")
    df = df.set_index("date")
    df.index = pd.to_datetime(df.index)
    return df[["open", "high", "low", "close", "volume"]]


def fetch_intraday_bars(ib: IB, symbol: str, bar_minutes: int, duration: str = "2 D") -> pd.DataFrame:
    """Fetch intraday historical bars (e.g. for an opening-range strategy)
    as a DataFrame indexed by US/Eastern timestamps — the ORB strategy
    identifies "today's session" via `.index.date`, which must line up
    with the ET trading day, not UTC (formatDate=2 gives UTC; converted
    here). `duration` spans more than one day so a strategy that looks at
    "today's session" can tell today's bars apart from a stale prior day
    if called right at the open, before today has any bars of its own yet.
    """
    contract = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(contract)
    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting=f"{bar_minutes} mins",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=2,  # UTC; converted to America/New_York below
    )
    df = util.df(bars)
    if df is None or df.empty:
        raise ValueError(f"No intraday historical data returned for {symbol}")
    df = df.set_index("date")
    df.index = pd.to_datetime(df.index, utc=True).tz_convert("America/New_York")
    return df[["open", "high", "low", "close", "volume"]]
