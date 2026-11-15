import pytest

from tradingbot.config import settings


@pytest.fixture(autouse=True)
def _isolated_control_file(tmp_path, monkeypatch):
    """Every test gets its own (absent) control file: the real
    data/control.json — a strategy paused from the dashboard — must never
    change what a test sees."""
    monkeypatch.setattr(settings, "control_path", str(tmp_path / "control.json"))
