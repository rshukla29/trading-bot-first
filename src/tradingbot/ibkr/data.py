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
