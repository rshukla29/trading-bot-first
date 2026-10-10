from __future__ import annotations

import logging
from dataclasses import dataclass

from ib_async import IB, ComboLeg, Contract, LimitOrder, Option, Trade

from tradingbot.options.live_chain import get_quote, get_quote_lenient, listed_strikes, nearest_expiry, qualify_option
from tradingbot.options.pricing import find_strike_for_delta

logger = logging.getLogger(__name__)


@dataclass
class CondorLegs:
    call_short: Option
    call_long: Option
    put_short: Option
    put_long: Option
    expiry: str


def select_condor_legs(
    ib: IB, symbol: str, chain, spot: float, vol: float, rate: float,
    target_delta: float, wing_width: float, dte_days: int,
) -> CondorLegs:
    """Pick real, listed strikes closest to the target delta. Delta itself
    is estimated from Black-Scholes with a realized-vol proxy (same method
    already validated in backtesting) rather than IBKR's live modelGreeks,
    which weren't reliably populating as of this build — strike selection
    only needs to be approximately right, since actual order pricing below
    uses real live bid/ask, not this estimate.
    """
    expiry = nearest_expiry(chain, dte_days)
    t_years = dte_days / 365.25

    raw_call_short = find_strike_for_delta(spot, t_years, rate, vol, "C", target_delta)
    raw_put_short = find_strike_for_delta(spot, t_years, rate, vol, "P", target_delta)

    # Strikes come from what's actually listed for this expiry (listed_strikes):
    # chain.strikes is a union across all expirations, and probing it for
    # strikes that don't exist raised IBKR error 200 on every run.
    calls = listed_strikes(ib, symbol, expiry, "C")
    puts = listed_strikes(ib, symbol, expiry, "P")
    if not calls or not puts:
        raise ValueError(f"{symbol}: no listed strikes for {expiry}")

    def nearest(strikes, target):
        return min(strikes, key=lambda k: abs(k - target))

    cs = nearest(calls, raw_call_short)
    ps = nearest(puts, raw_put_short)
    call_short = qualify_option(ib, symbol, expiry, cs, "C")
    put_short = qualify_option(ib, symbol, expiry, ps, "P")
    call_long = qualify_option(ib, symbol, expiry, nearest(calls, cs + wing_width), "C")
    put_long = qualify_option(ib, symbol, expiry, nearest(puts, ps - wing_width), "P")

    # On a sparse strike ladder, the fallback above could in principle land
    # the "long" leg no farther out than the "short" leg — refuse rather
    # than silently build an inverted or zero-width spread.
    if not (call_long.strike > call_short.strike):
        raise ValueError(f"{symbol}: call spread not properly ordered ({call_short.strike} / {call_long.strike})")
    if not (put_long.strike < put_short.strike):
        raise ValueError(f"{symbol}: put spread not properly ordered ({put_short.strike} / {put_long.strike})")

    return CondorLegs(call_short=call_short, call_long=call_long, put_short=put_short, put_long=put_long, expiry=expiry)


def _combo_contract(symbol: str, legs: list[tuple[Option, str]]) -> Contract:
    """legs: list of (option_contract, action) — action is this leg's own
    BUY/SELL, independent of the parent order's action (always 'BUY' by
    IBKR combo convention — see module docstring in run_iron_condor_bot)."""
    combo = Contract(symbol=symbol, secType="BAG", currency="USD", exchange="SMART")
    combo.comboLegs = [
        ComboLeg(conId=opt.conId, ratio=1, action=action, exchange="SMART") for opt, action in legs
    ]
    return combo


def net_credit_estimate(ib: IB, legs: CondorLegs) -> float:
    """Sum of real mid-prices across all 4 legs — summing individual legs'
    live quotes rather than requesting a quote on the BAG contract itself,
    since 4-leg combo market data is often unreliable/non-streaming on
    IBKR even when the underlying legs quote fine individually."""
    _, _, call_short_mid = get_quote(ib, legs.call_short)
    _, _, call_long_mid = get_quote(ib, legs.call_long)
    _, _, put_short_mid = get_quote(ib, legs.put_short)
    _, _, put_long_mid = get_quote(ib, legs.put_long)
    return (call_short_mid - call_long_mid) + (put_short_mid - put_long_mid)


def wait_for_fill(ib: IB, trade: Trade, timeout: float = 90.0, poll_interval: float = 5.0) -> bool:
    """Block until `trade` fills or `timeout` elapses. Paper-account combo
    (BAG) fills observed taking up to ~60s even at a fully marketable
    price — much slower than single-leg orders — so a short poll window
    will misreport a soon-to-fill order as stuck. Returns True if filled
    by the deadline, False otherwise (caller decides whether to leave it
    working or cancel)."""
    elapsed = 0.0
    while elapsed < timeout:
        if trade.orderStatus.status == "Filled":
            return True
        if trade.orderStatus.status in ("Cancelled", "ApiCancelled", "Inactive"):
            return False
        ib.sleep(poll_interval)
        elapsed += poll_interval
    return trade.orderStatus.status == "Filled"


def open_condor(ib: IB, symbol: str, legs: CondorLegs, contracts: int, limit: float | None = None) -> tuple[Trade, float]:
    """Sells the condor for a net credit at `limit` (or the mid). Returns
    (Trade, credit_used_as_limit)."""
    credit = limit if limit is not None else net_credit_estimate(ib, legs)
    if credit <= 0.01:
        raise ValueError(f"{symbol}: computed credit {credit:.3f} too small/negative, refusing to open")

    combo = _combo_contract(symbol, [
        (legs.call_short, "SELL"), (legs.call_long, "BUY"),
        (legs.put_short, "SELL"), (legs.put_long, "BUY"),
    ])
    # tif must be set explicitly — LimitOrder defaults tif='' (unset), which
    # IBKR flags with "Order TIF was set to DAY based on order preset" and
    # then auto-corrects, but that correction/resubmit cycle was observed
    # to be unreliable (sometimes recovers to a working order, sometimes
    # the order just stays Cancelled) — this was very likely the real cause
    # of orders appearing to "not fill," not fundamental combo-order slowness.
    order = LimitOrder("BUY", contracts, round(-credit, 2), tif="DAY")  # negative lmtPrice = net credit
    trade = ib.placeOrder(combo, order)
    logger.info("%s: opened condor combo order, %d contracts, credit ~%.2f", symbol, contracts, credit)
    return trade, credit


def close_condor(ib: IB, symbol: str, legs: CondorLegs, contracts: int, limit: float | None = None) -> tuple[Trade, float]:
    """Buys back the condor (a debit) at `limit`, or the mid if not given.
    Returns (Trade, limit_used)."""
    cost = limit if limit is not None else net_credit_estimate(ib, legs)
    combo = _combo_contract(symbol, [
        (legs.call_short, "BUY"), (legs.call_long, "SELL"),
        (legs.put_short, "BUY"), (legs.put_long, "SELL"),
    ])
    order = LimitOrder("BUY", contracts, round(max(cost, 0.01), 2), tif="DAY")  # positive lmtPrice = net debit to close
    trade = ib.placeOrder(combo, order)
    logger.info("%s: close condor combo order, %d contracts, limit %.2f", symbol, contracts, cost)
    return trade, cost


def condor_close_ladder(ib: IB, legs: CondorLegs) -> list[float]:
    """Debits to try when buying a condor back, best first: the mid, halfway
    to the natural price, then the natural price (short legs at their ask,
    long legs at their bid; a long leg with no bid counts as 0). Mid-only
    pricing left a condor unclosed for days (2026-10-02 .. 10-08)."""
    q = {name: get_quote_lenient(ib, getattr(legs, name))
         for name in ("call_short", "call_long", "put_short", "put_long")}
    mid = sum(q[n][2] for n in ("call_short", "put_short")) - sum(q[n][2] for n in ("call_long", "put_long"))
    natural = (q["call_short"][1] + q["put_short"][1]) - (q["call_long"][0] + q["put_long"][0])
    return debit_ladder(mid, natural)


def debit_ladder(mid: float, natural: float) -> list[float]:
    """mid -> halfway -> natural, rounded, increasing, de-duplicated."""
    out = []
    for p in (mid, (mid + natural) / 2, natural):
        p = round(max(p, 0.01), 2)
        if not out or p > out[-1]:
            out.append(p)
    return out


OPEN_NATURAL_MIN_SHARE = 0.90


def condor_open_ladder(ib: IB, legs: CondorLegs) -> list[float]:
    """Credits to ask when opening, best first. See credit_ladder_for_open."""
    q = {name: get_quote(ib, getattr(legs, name)) for name in ("call_short", "call_long", "put_short", "put_long")}
    mid = sum(q[n][2] for n in ("call_short", "put_short")) - sum(q[n][2] for n in ("call_long", "put_long"))
    natural = (q["call_short"][0] + q["put_short"][0]) - (q["call_long"][1] + q["put_long"][1])
    return credit_ladder_for_open(mid, natural)


def credit_ladder_for_open(mid: float, natural: float, min_share: float = OPEN_NATURAL_MIN_SHARE) -> list[float]:
    """mid -> halfway -> natural credit, but the natural rung only when it
    is at least `min_share` of the mid (a tight market). 2026-10-09: mid
    1.25 and halfway 1.24 didn't fill on the paper account while the
    natural was ~1.23; on a wide market the natural rung is skipped so an
    open never gives away much of the credit."""
    rungs = [mid, (mid + natural) / 2]
    if mid > 0 and natural >= min_share * mid:
        rungs.append(natural)
    out = []
    for p in rungs:
        p = round(p, 2)
        if p > 0.01 and (not out or p < out[-1]):
            out.append(p)
    return out
