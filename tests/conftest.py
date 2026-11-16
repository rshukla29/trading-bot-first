import pytest

from tradingbot.config import settings


@pytest.fixture(autouse=True)
def _isolated_control_file(tmp_path, monkeypatch):
    """Every test gets its own (absent) control file: the real
    data/control.json — a strategy paused from the dashboard — must never
    change what a test sees."""
    monkeypatch.setattr(settings, "control_path", str(tmp_path / "control.json"))
    # Same for the update check's files (data/update_status.json, update_result.json).
    from tradingbot import updates
    monkeypatch.setattr(updates, "STATUS", tmp_path / "update_status.json")
    monkeypatch.setattr(updates, "RESULT", tmp_path / "update_result.json")
