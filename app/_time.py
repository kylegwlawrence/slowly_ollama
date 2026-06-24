"""Shared time helpers.

A tiny module so :mod:`app.queries`, :mod:`app.db`, and
:mod:`app.rag_servers` can all stamp ISO-8601 UTC timestamps from one
definition without each carrying their own copy.
"""

from datetime import datetime, timezone


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string for DB storage."""
    return datetime.now(timezone.utc).isoformat()


def today_utc() -> str:
    """Return today's UTC date as a plain ``YYYY-MM-DD`` string.

    Used to ground the model in the current date via the system prompt, so
    it doesn't answer time-sensitive questions from its frozen training
    knowledge. Date-only and UTC by design: the day is stable enough to be
    correct for a whole turn, where a wall-clock time would go stale mid-turn.

    Returns:
        The current UTC calendar date, e.g. ``"2026-06-23"``.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
