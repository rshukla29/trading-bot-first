"""ORB credit spreads (0DTE): the opening-range breakout used to SELL
premium on the far side of the range, instead of buying options.

Source: Option Alpha's and Quantish's published SPX 0DTE ORB research
(analysis/2026-10-01_orb_loss_analysis): 60-minute opening range; the
first 5-min close outside it between 10:30 and 12:00 sets the direction;
sell a credit spread anchored at the OPPOSITE side of the range (breakout
up -> put spread with the short put at/below the range low; breakout down
-> call spread with the short call at/above the range high); hold to the
close. Published SPX results: ~87-89% win rate, PF 1.3-1.44.

Why this structure: a bought 0DTE option loses value every minute, so it
needs a near-perfect exit; a credit spread earns that time decay and is
only hurt if price reverses all the way through the range.

SPY adaptation: SPY options settle in shares, so positions are closed at
3:55pm rather than left to expire (the 3:57pm expiry guard backs this up).

Pure logic only (no IBKR): shared by the backtest below and the live bot.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import time

import pandas as pd

from tradingbot.options.pricing import bs_price

CONTRACT_MULTIPLIER = 100
MINUTES_PER_YEAR = 365.25 * 24 * 60


@dataclass(frozen=True)
class CreditOrbConfig:
    range_minutes: int = 60
    bar_minutes: int = 5
    entry_start: time | None = None  # default: when the range completes
    entry_end: time = time(12, 0)
    min_range_pct: float = 0.002     # range must be at least 0.2% of the open
    width: float = 2.0               # SPY $2 ~ SPX $15 (0.25% of price)
    strike_increment: float = 1.0
    anchor: str = "range"            # "range": short strike at the far range edge; "mid": at the range midpoint
    exit_time: time = time(15, 55)
    skip_weekdays: tuple[int, ...] = ()  # 0=Mon ... 4=Fri
    max_loss_dollars: float = 1000.0     # per trade, sizes the contracts
    max_contracts: int = 10
    slippage_per_leg: float = 0.02
    commission_per_leg: float = 0.65
    profit_take_pct: float | None = None  # close early once this share of the credit is captured
    rate: float = 0.04


@dataclass(frozen=True)
class OrbSignal:
    direction: str     # "up" -> sell put spread, "down" -> sell call spread
    bar_ts: pd.Timestamp
    range_low: float
    range_high: float
    spot: float


def find_breakout(day_bars: pd.DataFrame, cfg: CreditOrbConfig) -> OrbSignal | None:
    """First completed bar closing outside the opening range within the
    entry window, on a day whose range is wide enough. `day_bars`: one
    session's regular-hours bars, ET-indexed by bar start, completed only."""
    n_range = cfg.range_minutes // cfg.bar_minutes
    if len(day_bars) <= n_range:
        return None
    rng = day_bars.iloc[:n_range]
    lo, hi = float(rng["low"].min()), float(rng["high"].max())
    if (hi - lo) < cfg.min_range_pct * float(rng["open"].iloc[0]):
        return None
    start = cfg.entry_start or (day_bars.index[n_range - 1] + pd.Timedelta(minutes=cfg.bar_minutes)).time()
    for ts, bar in day_bars.iloc[n_range:].iterrows():
        closed_at = (ts + pd.Timedelta(minutes=cfg.bar_minutes)).time()
        if closed_at < start:
            continue
        if closed_at > cfg.entry_end:
            return None
        c = float(bar["close"])
        if c > hi:
            return OrbSignal("up", ts, lo, hi, c)
        if c < lo:
            return OrbSignal("down", ts, lo, hi, c)
    return None


def spread_strikes(sig: OrbSignal, cfg: CreditOrbConfig) -> tuple[str, float, float]:
    """(right, short_strike, long_strike). Short strike sits on the far side
    of the range (or its midpoint), never closer to price than that."""
    inc = cfg.strike_increment
    if sig.direction == "up":
        level = sig.range_low if cfg.anchor == "range" else (sig.range_low + sig.range_high) / 2
        short = math.floor(level / inc) * inc
        return "P", short, short - cfg.width
    level = sig.range_high if cfg.anchor == "range" else (sig.range_low + sig.range_high) / 2
    short = math.ceil(level / inc) * inc
    return "C", short, short + cfg.width


def spread_value(spot: float, right: str, short: float, long: float, minutes_left: float, vol: float, rate: float) -> float:
    """Cost to buy the spread back (short leg value minus long leg value)."""
    t = max(minutes_left, 0.0) / MINUTES_PER_YEAR
    return max(0.0, bs_price(spot, short, t, rate, vol, right) - bs_price(spot, long, t, rate, vol, right))


def credit_limit_ladder(short_bid: float, short_ask: float, long_bid: float, long_ask: float,
                        floor: float) -> list[float]:
    """Asking credits to try, best first: the mid, halfway to the natural
    price, then the natural price (short leg at its bid, long leg at its
    ask) — never below `floor`. 2026-10-02: asking only the mid (0.13, then
    0.11) on a cheap SPY call spread didn't fill. Duplicates (a tight
    market where mid == natural) collapse to one attempt."""
    mid = (short_bid + short_ask) / 2 - (long_bid + long_ask) / 2
    natural = short_bid - long_ask
    ladder = []
    for price in (mid, (mid + natural) / 2, natural):
        p = round(price, 2)
        if p >= floor and p not in ladder:
            ladder.append(p)
    return ladder


def contracts_for(credit: float, cfg: CreditOrbConfig) -> int:
    max_loss_per = (cfg.width - credit) * CONTRACT_MULTIPLIER
    if max_loss_per <= 0:
        return 0
    return max(0, min(cfg.max_contracts, int(cfg.max_loss_dollars // max_loss_per)))


@dataclass
class CreditTrade:
    day: str
    direction: str
    entry_ts: pd.Timestamp
    right: str
    short: float
    long: float
    credit: float
    contracts: int
    exit_ts: pd.Timestamp | None = None
    exit_cost: float | None = None
    exit_reason: str | None = None
    fees: float = 0.0

    @property
    def pnl(self) -> float | None:
        if self.exit_cost is None:
            return None
        return (self.credit - self.exit_cost) * self.contracts * CONTRACT_MULTIPLIER - self.fees


def _minutes_left(ts_close: pd.Timestamp) -> float:
    close = ts_close.normalize() + pd.Timedelta(hours=16)
    return (close - ts_close).total_seconds() / 60


def backtest_credit_orb(bars: pd.DataFrame, vol: pd.Series, cfg: CreditOrbConfig) -> list[CreditTrade]:
    """One trade per day at most. `vol`: annualized implied-vol estimate per
    bar (e.g. pricing.vix_vol_series), known at each bar's close."""
    trades: list[CreditTrade] = []
    step = pd.Timedelta(minutes=cfg.bar_minutes)
    for day, day_bars in bars.groupby(bars.index.date):
        if day_bars.index[0].weekday() in cfg.skip_weekdays:
            continue
        sig = find_breakout(day_bars, cfg)
        if sig is None:
            continue
        v = vol.get(sig.bar_ts)
        if v is None or pd.isna(v):
            continue
        right, short, long = spread_strikes(sig, cfg)
        entry_close = sig.bar_ts + step
        value = spread_value(sig.spot, right, short, long, _minutes_left(entry_close), v, cfg.rate)
        credit = value - 2 * cfg.slippage_per_leg  # sell short leg at bid, buy long leg at ask
        if credit <= 0.05:
            continue
        n = contracts_for(credit, cfg)
        if n == 0:
            continue
        tr = CreditTrade(str(day), sig.direction, entry_close, right, short, long, credit, n)
        fees_per_leg_trade = cfg.commission_per_leg * n

        for ts, bar in day_bars[day_bars.index > sig.bar_ts].iterrows():
            ts_close = ts + step
            vv = vol.get(ts, v)
            now_value = spread_value(float(bar["close"]), right, short, long, _minutes_left(ts_close), vv, cfg.rate)
            reason = None
            if ts_close.time() >= cfg.exit_time:
                reason = "close"
            elif cfg.profit_take_pct is not None and now_value <= credit * (1 - cfg.profit_take_pct):
                reason = "profit_take"
            if reason:
                # Near-worthless spreads are bought back for ~nothing; otherwise pay the spread.
                tr.exit_cost = now_value + (2 * cfg.slippage_per_leg if now_value > 0.02 else now_value)
                tr.exit_ts, tr.exit_reason = ts_close, reason
                tr.fees = 4 * fees_per_leg_trade
                break
        if tr.exit_cost is not None:
            trades.append(tr)
    return trades


def summarize(trades: list[CreditTrade]) -> dict:
    pnl = [t.pnl for t in trades]
    wins = [p for p in pnl if p > 0]
    gl = -sum(p for p in pnl if p <= 0)
    equity = pd.Series(pnl).cumsum() + 100_000 if pnl else pd.Series([100_000.0])
    dd = float((equity / equity.cummax() - 1).min())
    return dict(trades=len(pnl), win=round(100 * len(wins) / max(len(pnl), 1), 1), net=round(sum(pnl)),
                pf=round(sum(wins) / gl, 2) if gl else float("inf"),
                avg_win=round(sum(wins) / max(len(wins), 1)), avg_loss=round(-gl / max(len(pnl) - len(wins), 1)),
                max_dd=round(100 * dd, 1))
