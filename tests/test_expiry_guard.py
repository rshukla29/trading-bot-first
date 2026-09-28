from datetime import date
from types import SimpleNamespace

from tradingbot.run_expiry_guard import closing_order, expiring_today

TODAY = date(2026, 9, 28)


def _pos(sec_type, expiry, qty, symbol="IWM"):
    return SimpleNamespace(
        contract=SimpleNamespace(secType=sec_type, lastTradeDateOrContractMonth=expiry, symbol=symbol),
        position=qty,
    )


def test_selects_only_options_expiring_today():
    iwm_0dte = _pos("OPT", "20260928", 10)
    condor_leg = _pos("OPT", "20261023", -1, "SPY")  # iron condor, ~25 DTE
    stock = _pos("STK", "", 36, "XLF")
    flat = _pos("OPT", "20260928", 0, "SPY")
    assert expiring_today([iwm_0dte, condor_leg, stock, flat], TODAY) == [iwm_0dte]


def test_long_is_sold_short_is_bought_back_full_quantity():
    long_order = closing_order(_pos("OPT", "20260928", 10))
    short_order = closing_order(_pos("OPT", "20260928", -3))
    assert (long_order.action, long_order.totalQuantity) == ("SELL", 10)
    assert (short_order.action, short_order.totalQuantity) == ("BUY", 3)
    assert long_order.orderType == "MKT" and long_order.tif == "DAY"
