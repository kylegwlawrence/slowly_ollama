"""Built-in tools shipped with phase 12.

Currently only one: `current_time`, the baseline that validates the
tool-calling loop without depending on any external service.
"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.tools import tool


@tool
def current_time(timezone: str = "UTC") -> str:
    """Get the current wall-clock time as ISO 8601. Only call this when the user explicitly asks for the date/time or a calculation genuinely depends on "now"; never as a default, warm-up, or speculative call.

    Args:
        timezone: IANA timezone name like "America/Vancouver" or "UTC".
            Defaults to "UTC". Unknown names fall back to UTC and
            include a note in the returned string.
    """
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        # Don't raise — the model just sees the error string and can
        # retry with a valid timezone. Always include the actual time
        # so the call isn't a total loss.
        return f"Unknown timezone '{timezone}'; defaulted to UTC. Now: {datetime.now(ZoneInfo('UTC')).isoformat()}"
    return datetime.now(tz).isoformat()
