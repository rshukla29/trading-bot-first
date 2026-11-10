"""2026-11-09: IBKR error 10197 (live account logged in elsewhere) sent
SPY bid=nan ask=nan all day. Exit checks must fall back to historical
bars, and a close with no live option quote must go out at market."""
from types import SimpleNamespace

import pandas as pd
from ib_async import BarData, MarketOrder

import tradingbot.run_orb_bot as orb
from tradingbot.options import live_orb


class _IB:
    """Streams: SPY (conId 1) never quotes; the option (conId 2) does
    unless option_live=False. Historical 1-min bars always work."""

    def __init__(self, spy_bar, opt_bar, option_live=True):
        self.bars = {1: spy_bar, 2: opt_bar}
        self.option_live = option_live
        self.hist, self.orders = [], []

    def reqMktData(self, contract, genericTickList="", snapshot=False):
        live = contract.conId == 2 and self.option_live
        return SimpleNamespace(bid=2.0 if live else float("nan"), ask=2.2 if live else float("nan"))

    def cancelMktData(self, contract):
        pass

    def sleep(self, s):
        pass

    def reqHistoricalData(self, contract, end, duration, size, what, rth, formatDate=2):
        self.hist.append((contract.conId, what))
        return [BarData(date=pd.Timestamp("2026-11-09 15:00"), close=self.bars[contract.conId])]

    def placeOrder(self, contract, order):
        self.orders.append(order)
        return SimpleNamespace(order=order, orderStatus=SimpleNamespace(status="Filled", avgFillPrice=5.17, filled=5))


SPY = SimpleNamespace(conId=1, secType="STK", localSymbol="SPY")
OPT = SimpleNamespace(conId=2, secType="OPT", localSymbol="SPY   261109C00748000")


def setup_function():
    orb._streams.clear()
    orb._fallback_warned.clear()


def test_price_falls_back_to_bar_close_and_warns_once(caplog):
    ib = _IB(spy_bar=752.6, opt_bar=4.9)
    assert orb._price(ib, SPY) == 752.6
    assert ib.hist == [(1, "TRADES")]
    assert orb._price(ib, OPT) == 2.1           # option's live quote still used
    orb._price(ib, SPY)
    assert sum("using the last 1-min bar close" in r.message for r in caplog.records) == 1


def test_exit_without_live_option_quote_sells_at_market():
    ib = _IB(spy_bar=752.6, opt_bar=4.9, option_live=False)
    trade, price = live_orb.exit_orb_trade(ib, OPT, 5, quote=orb._quote)
    assert isinstance(trade.order, MarketOrder) and trade.order.totalQuantity == 5
    assert price != price  # nan


def test_check_exit_works_with_spy_quote_blacked_out(monkeypatch):
    """Today's trade: 748C x5 from 1.93, range 741.02-746.80, SPY past the
    200% Fibonacci extension — with no live SPY quote it must still exit."""
    ib = _IB(spy_bar=759.0, opt_bar=11.0, option_live=False)
    closed = []
    journal = SimpleNamespace(update_orb_peak=lambda *a: None, log_order=lambda *a, **k: None,
                              close_orb_position=lambda *a: closed.append(a))
    monkeypatch.setattr(orb, "held_qty", lambda ib, con_id: 5)
    monkeypatch.setattr(orb, "_now_et", lambda: pd.Timestamp("2026-11-09 14:00", tz="America/New_York").to_pydatetime())
    position = dict(id=30, contract=OPT, contracts=5, entry_premium=1.93, right="C",
                    range_low=741.02, range_high=746.80)
    assert orb.check_exit(ib, journal, "SPY", SPY, position) is True
    assert isinstance(ib.orders[0], MarketOrder)
    assert closed and closed[0][2] == 5.17
