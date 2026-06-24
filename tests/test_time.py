"""Tests for the shared time helpers in :mod:`app._time`."""

import re
from datetime import datetime, timezone

from app._time import today_utc


def test_today_utc_format() -> None:
    """``today_utc`` returns a plain ``YYYY-MM-DD`` string, no time part."""
    value = today_utc()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", value), value


def test_today_utc_matches_current_utc_date() -> None:
    """The returned date is today's UTC calendar date."""
    assert today_utc() == datetime.now(timezone.utc).strftime("%Y-%m-%d")
