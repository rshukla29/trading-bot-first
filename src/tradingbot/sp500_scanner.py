"""S&P 500 scanner using yfinance (read-only, no IBKR connection, no orders).

Pulls the current S&P 500 constituent list, downloads ~2 years of daily
bars for all ~500 names from Yahoo Finance in one batched request, and
reports:
  - top gainers / losers on the latest bar
  - unusual volume (latest volume vs. the prior 20-bar average)
  - which names both live stock strategies (MA-crossover, RSI(2)
    mean-reversion) would BUY right now

    python -m tradingbot.sp500_scanner
    python -m tradingbot.sp500_scanner --top 20 --csv reports/sp500_scan.csv

Caveats:
  - Run during market hours, the latest bar is today's partial session
    (Yahoo updates it live, ~15 min delayed) — same "unfinalized bar"
    issue the scheduled bots avoid by running after the close.
  - Prices are dividend- and split-adjusted (auto_adjust=True); IBKR's
    TRADES bars are split-adjusted only, so indicator values can differ
    slightly from what the live bots compute on the same symbol.
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from tradingbot.config import settings
from tradingbot.indicators import rsi, volume_spike_ratio
from tradingbot.strategy.base import Strategy
from tradingbot.strategy.factory import build_strategy
from tradingbot.strategy.rsi2_mean_reversion import RSI2MeanReversionStrategy

log = logging.getLogger(__name__)

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
CONSTITUENTS_CACHE = Path("data/sp500_constituents.csv")
CACHE_MAX_AGE = timedelta(days=7)
BAR_COLUMNS = ["open", "high", "low", "close", "volume"]
UNUSUAL_VOLUME_RATIO = 2.0


def to_yahoo_symbol(symbol: str) -> str:
    """Wikipedia/exchange class-share notation (BRK.B) -> Yahoo's (BRK-B)."""
    return symbol.strip().replace(".", "-")


def parse_constituents_html(html: str) -> pd.DataFrame:
    """Extract symbol/name/sector from the Wikipedia constituents table."""
    table = pd.read_html(StringIO(html), attrs={"id": "constituents"})[0]
    df = pd.DataFrame({
        "symbol": table["Symbol"].map(to_yahoo_symbol),
        "name": table["Security"],
        "sector": table["GICS Sector"],
    })
    return df.drop_duplicates("symbol").reset_index(drop=True)


def fetch_sp500_constituents(cache_path: Path = CONSTITUENTS_CACHE, refresh: bool = False) -> pd.DataFrame:
    """Current S&P 500 members, cached locally for a week (index changes
    are rare). Falls back to a stale cache if Wikipedia is unreachable."""
    cache_fresh = cache_path.exists() and (
        datetime.now() - datetime.fromtimestamp(cache_path.stat().st_mtime) < CACHE_MAX_AGE
    )
    if cache_fresh and not refresh:
        return pd.read_csv(cache_path)

    try:
        resp = requests.get(WIKI_URL, headers={"User-Agent": "Mozilla/5.0 (tradingbot sp500 scanner)"}, timeout=20)
        resp.raise_for_status()
        df = parse_constituents_html(resp.text)
    except Exception as e:
        if cache_path.exists():
            log.warning("Constituent refresh failed (%s); using stale cache %s", e, cache_path)
            return pd.read_csv(cache_path)
        raise

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache_path, index=False)
    return df


def download_daily_bars(symbols: list[str], period: str = "2y") -> dict[str, pd.DataFrame]:
    """Batch-download daily bars; returns {symbol: DataFrame[open..volume]}
    in the same shape ibkr.data.fetch_daily_bars returns, so the existing
    strategies run on it unchanged. Symbols with no data are dropped."""
    raw = yf.download(
        symbols, period=period, interval="1d", group_by="ticker",
        auto_adjust=True, threads=True, progress=False,
    )
    out: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        if symbol not in raw.columns.get_level_values(0):
            continue
        df = raw[symbol].rename(columns=str.lower)[BAR_COLUMNS].dropna()
        if not df.empty:
            out[symbol] = df
    return out


def build_strategies() -> dict[str, Strategy]:
    return {
        "MA-crossover": build_strategy(settings),
        "RSI(2)": RSI2MeanReversionStrategy(
            trend_ma=settings.rsi2_trend_ma,
            rsi_period=settings.rsi2_period,
            buy_threshold=settings.rsi2_buy_threshold,
            sell_threshold=settings.rsi2_sell_threshold,
            fib_lookback=settings.rsi2_fib_lookback or None,
            fib_zone=(settings.rsi2_fib_zone_low, settings.rsi2_fib_zone_high),
        ),
    }


def scan(bars_by_symbol: dict[str, pd.DataFrame], constituents: pd.DataFrame,
         strategies: dict[str, Strategy]) -> pd.DataFrame:
    """One row per symbol with latest-bar stats and each strategy's BUY
    reason (empty string when it's not a BUY)."""
    meta = constituents.set_index("symbol")
    rows = []
    for symbol, bars in bars_by_symbol.items():
        if len(bars) < 2:
            continue
        close, prev_close = bars["close"].iloc[-1], bars["close"].iloc[-2]
        row = {
            "symbol": symbol,
            "name": meta["name"].get(symbol, ""),
            "sector": meta["sector"].get(symbol, ""),
            "date": bars.index[-1].date(),
            "close": round(float(close), 2),
            "change_pct": round(float((close / prev_close - 1) * 100), 2),
            "volume": int(bars["volume"].iloc[-1]),
            "volume_ratio": round(float(volume_spike_ratio(bars["volume"], settings.rsi2_volume_spike_window).iloc[-1]), 2),
            "rsi2": round(float(rsi(bars["close"], settings.rsi2_period).iloc[-1]), 1),
        }
        for label, strategy in strategies.items():
            reason = ""
            if len(bars) >= strategy.min_history:
                signal = strategy.generate_signal(symbol, bars)
                if signal.action == "BUY":
                    reason = signal.reason
            row[label] = reason
        rows.append(row)
    return pd.DataFrame(rows)


def _print_table(title: str, df: pd.DataFrame, columns: list[str]) -> None:
    print(f"\n=== {title} ({len(df)}) ===")
    if df.empty:
        print("(none)")
    else:
        print(df[columns].to_string(index=False))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="S&P 500 scanner (yfinance, read-only)")
    parser.add_argument("--top", type=int, default=15, help="rows per gainers/losers/volume table")
    parser.add_argument("--csv", type=Path, help="also write the full scan to this CSV path")
    parser.add_argument("--refresh-constituents", action="store_true", help="ignore the 7-day constituent cache")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

    constituents = fetch_sp500_constituents(refresh=args.refresh_constituents)
    symbols = constituents["symbol"].tolist()
    print(f"Downloading daily bars for {len(symbols)} S&P 500 constituents from Yahoo Finance...")
    bars = download_daily_bars(symbols)
    missing = sorted(set(symbols) - set(bars))
    if missing:
        print(f"No data for {len(missing)} symbols: {', '.join(missing)}")

    results = scan(bars, constituents, build_strategies())
    as_of = results["date"].max()
    print(f"Scanned {len(results)} symbols, latest bar {as_of}")

    move_cols = ["symbol", "name", "close", "change_pct", "volume_ratio", "rsi2"]
    by_change = results.sort_values("change_pct", ascending=False)
    _print_table("Top gainers", by_change.head(args.top), move_cols)
    _print_table("Top losers", by_change.tail(args.top).iloc[::-1], move_cols)
    unusual = results[results["volume_ratio"] >= UNUSUAL_VOLUME_RATIO].sort_values("volume_ratio", ascending=False)
    _print_table(f"Unusual volume (>= {UNUSUAL_VOLUME_RATIO}x 20-day avg)", unusual.head(args.top), move_cols)

    for label in build_strategies():
        hits = results[results[label] != ""]
        _print_table(f"{label} BUY signals", hits, ["symbol", "name", "close", "change_pct", "volume_ratio", label])

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        results.to_csv(args.csv, index=False)
        print(f"\nFull scan written to {args.csv}")


if __name__ == "__main__":
    main()
