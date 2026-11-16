"""8am jobs wait for TWS (connect_with_retry), and Bot health doesn't call a
refused connection an error once a later connection succeeded."""
import pytest

from tradingbot.dashboard import _classify
from tradingbot.ibkr.client import connect_with_retry


class FakeIB:
    def __init__(self, fail_times):
        self.fail_times, self.calls, self.disconnects = fail_times, 0, 0

    def connect(self, host, port, clientId, timeout, **kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionRefusedError(22, "refused")

    def disconnect(self):
        self.disconnects += 1


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_waits_for_tws_to_come_up():
    ib, clock = FakeIB(fail_times=3), Clock()
    connect_with_retry(ib, "127.0.0.1", 7497, 99, wait_minutes=10, sleep=clock.sleep, clock=clock)
    assert ib.calls == 4 and clock.t == 90  # three 30s waits


def test_gives_up_after_the_wait():
    ib, clock = FakeIB(fail_times=1000), Clock()
    with pytest.raises(ConnectionRefusedError):
        connect_with_retry(ib, "127.0.0.1", 7497, 99, wait_minutes=2, sleep=clock.sleep, clock=clock)
    assert clock.t == 120 and ib.calls == 5


def test_no_wait_fails_straight_away():
    ib, clock = FakeIB(fail_times=1), Clock()
    with pytest.raises(ConnectionRefusedError):
        connect_with_retry(ib, "127.0.0.1", 7497, 99, wait_minutes=0, sleep=clock.sleep, clock=clock)
    assert ib.calls == 1 and clock.t == 0


REFUSED = ["2026-11-16 08:00:08,945 ERROR API connection failed: ConnectionRefusedError(22, ...)",
           "2026-11-16 08:00:08,945 ERROR Make sure API port on TWS/IBG is open"]


def test_refused_then_connected_is_a_warning():
    lines = REFUSED + ["2026-11-16 08:09:01,000 INFO Connected", "2026-11-16 08:09:02,000 INFO Connected to IBKR at 127.0.0.1:7497"]
    errors, warnings = _classify("snapshot_premarket", lines)
    assert errors == [] and len(warnings) == 2


def test_snapshot_rerun_that_saved_its_report_is_a_warning():
    lines = REFUSED + ["Saved to reports\\DUR356762\\2026\\11\\16\\snapshot_2026-11-16_premarket.html"]
    errors, warnings = _classify("snapshot_premarket", lines)
    assert errors == [] and len(warnings) == 2


def test_refused_and_never_connected_is_an_error():
    errors, _ = _classify("snapshot_premarket", REFUSED)
    assert len(errors) == 2


def test_readiness_log_never_counts_as_errors():
    errors, warnings = _classify("readiness", REFUSED)
    assert errors == [] and len(warnings) == 2


def test_a_real_error_after_connecting_still_counts():
    lines = REFUSED + ["2026-11-16 08:09:01,000 INFO Connected", "2026-11-16 08:09:05,000 ERROR Snapshot failed: boom"]
    errors, warnings = _classify("snapshot_premarket", lines)
    assert errors == ["2026-11-16 08:09:05,000 ERROR Snapshot failed: boom"] and len(warnings) == 2
