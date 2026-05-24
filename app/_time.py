"""Shared time helpers.

A tiny module so :mod:`app.queries`, :mod:`app.db`, and
:mod:`app.rag_servers` can all stamp ISO-8601 UTC timestamps from one
definition without each carrying their own copy.
"""

from datetime import datetime, timezone


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string for DB storage."""
    return datetime.now(timezone.utc).isoformat()
