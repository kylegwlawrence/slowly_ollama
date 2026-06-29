"""Shared formatting helpers.

Keeps byte-count formatting identical between the file tools
(``app.tools.builtins``) and the Files-tab routes (``app.routes``).
"""


def format_size_bytes(size_bytes: int) -> str:
    """Format a byte count as a compact human-readable string.

    Args:
        size_bytes: File size in bytes.

    Returns:
        A compact string like ``"4 B"``, ``"1.2 KB"``, or ``"3.4 MB"``.
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def format_duration_ms(duration_ms: int) -> str:
    """Format a millisecond duration as a compact ``Mmin Ss`` / ``Ss`` string.

    Rounds to whole seconds (most LLM turns run for seconds, not fractions),
    then splits into minutes + seconds. Minutes are the largest unit, so a
    long turn reads e.g. ``"75min 3s"`` rather than rolling into hours.

    Args:
        duration_ms: Wall-clock duration in milliseconds.

    Returns:
        A string like ``"32s"`` (under a minute) or ``"10min 32s"``.
    """
    total_seconds = round(duration_ms / 1000)
    minutes, seconds = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes}min {seconds}s"
    return f"{seconds}s"
