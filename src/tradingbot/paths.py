"""Dated folder layout shared by generated outputs (reports, scans)."""
from __future__ import annotations

from datetime import date
from pathlib import Path


def dated_dir(base: Path, day: date, per_day: bool = True) -> Path:
    """base/YYYY/MM/DD (per_day) or base/YYYY/MM (month level)."""
    month_dir = Path(base) / f"{day.year:04d}" / f"{day.month:02d}"
    return month_dir / f"{day.day:02d}" if per_day else month_dir
