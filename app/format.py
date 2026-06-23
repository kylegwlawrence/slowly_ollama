"""Shared formatting helpers.

So the file tools (``app.tools.builtins``) and the Files-tab routes
(``app.routes``) format byte counts the same way.
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
