from __future__ import annotations

from datetime import date, timedelta

from ib_async import IB, Option, Stock, Ticker


def get_real_chain(ib: IB, symbol: str):
    """The real, tradeable option chain for `symbol` on SMART.

    IBKR returns *multiple* chain entries for SMART/exchange combos — for
    SPY specifically, one with tradingClass="SPY" (the real chain: ~34
    expirations, ~483 strikes) and a decoy with tradingClass="2SPY" (a
    stub/mini product with only 2 expirations, 2-3 strikes). Naively taking
    "the first SMART entry" silently grabs the wrong one — this cost real
    debugging time before the fix. Filter on tradingClass == symbol
    explicitly.
    """
    underlying = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(underlying)
    chains = ib.reqSecDefOptParams(underlying.symbol, "", underlying.secType, underlying.conId)
    chain = next((c for c in chains if c.exchange == "SMART" and c.tradingClass == symbol), None)
    if chain is None:
        raise ValueError(f"No real (non-stub) SMART option chain found for {symbol}")
    return chain


def nearest_expiry(chain, target_dte_days: int, today: date | None = None) -> str:
    today = today or date.today()
    target = (today + timedelta(days=target_dte_days)).strftime("%Y%m%d")
    return min(chain.expirations, key=lambda e: abs(int(e) - int(target)))


def nearest_strike(chain, target_strike: float) -> float:
    return min(chain.strikes, key=lambda s: abs(s - target_strike))


def strikes_by_distance(chain, target_strike: float) -> list[float]:
    """chain.strikes is a flattened union across *all* expirations — a
    strike close to the target may simply not exist for the specific
    expiry being traded. Returns candidates ordered nearest-first so
    callers can fall back to the next-closest strike on a qualification
    failure."""
    return sorted(chain.strikes, key=lambda s: abs(s - target_strike))


def get_quote(ib: IB, contract, wait_seconds: float = 3.0) -> tuple[float, float, float]:
    """(bid, ask, mid) for a qualified contract. Raises if no valid quote
    arrives — callers must not silently trade on a missing quote."""
    ticker: Ticker = ib.reqMktData(contract, genericTickList="", snapshot=False)
    ib.sleep(wait_seconds)
    bid, ask = ticker.bid, ticker.ask
    ib.cancelMktData(contract)
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        raise ValueError(f"No valid live quote for {contract.localSymbol or contract}: bid={bid} ask={ask}")
    return bid, ask, (bid + ask) / 2


def no_bid_quote(bid, ask) -> tuple[float, float, float] | None:
    """A near-worthless option often has no bid at all (IBKR bid = -1) but a
    valid ask (e.g. 0.01). Treat that as bid 0, not as a missing quote —
    on 2026-10-08 the strict check made a credit spread unclosable. None if
    the ask isn't valid either."""
    if ask is None or ask != ask or ask <= 0:
        return None
    b = bid if (bid is not None and bid == bid and bid > 0) else 0.0
    return b, ask, (b + ask) / 2


def get_quote_lenient(ib: IB, contract, wait_seconds: float = 3.0) -> tuple[float, float, float]:
    """(bid, ask, mid) like get_quote, but a missing bid counts as 0 when
    the ask is valid. Raises only if the ask is missing too. For valuing and
    closing positions, not for opening new ones."""
    ticker: Ticker = ib.reqMktData(contract, genericTickList="", snapshot=False)
    ib.sleep(wait_seconds)
    bid, ask = ticker.bid, ticker.ask
    ib.cancelMktData(contract)
    q = no_bid_quote(bid, ask)
    if q is None:
        raise ValueError(f"No valid live quote for {contract.localSymbol or contract}: bid={bid} ask={ask}")
    return q


def listed_strikes(ib: IB, symbol: str, expiry: str, right: str) -> list[float]:
    """Strikes actually listed for one expiry and right (chain.strikes is a
    union across all expiries, so probing it raises IBKR error 200 for
    strikes that don't exist on the requested date)."""
    details = ib.reqContractDetails(Option(symbol, expiry, 0.0, right, "SMART", tradingClass=symbol))
    return sorted({d.contract.strike for d in details})


def qualify_option(ib: IB, symbol: str, expiry: str, strike: float, right: str) -> Option:
    opt = Option(symbol, expiry, strike, right, "SMART", tradingClass=symbol)
    # A contract that fails to resolve comes back as a `None` entry in the
    # list (not an empty list) — ib.qualifyContracts returns one slot per
    # input regardless of success.
    qualified = ib.qualifyContracts(opt)
    if not qualified or qualified[0] is None or qualified[0].conId == 0:
        raise ValueError(f"Could not qualify option contract: {symbol} {expiry} {strike}{right}")
    return qualified[0]


def qualify_nearest_available(
    ib: IB, symbol: str, expiry: str, target_strike: float, right: str, chain, max_attempts: int = 8,
) -> Option:
    """qualify_option with fallback: tries progressively farther strikes
    (from strikes_by_distance) until one actually exists for this expiry,
    since chain.strikes doesn't guarantee every strike is listed for every
    expiration."""
    candidates = strikes_by_distance(chain, target_strike)[:max_attempts]
    last_error: Exception | None = None
    for strike in candidates:
        try:
            return qualify_option(ib, symbol, expiry, strike, right)
        except ValueError as e:
            last_error = e
            continue
    raise ValueError(
        f"Could not qualify any of the {len(candidates)} nearest strikes to {target_strike} "
        f"for {symbol} {expiry}{right}: {last_error}"
    )
