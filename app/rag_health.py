"""RAG server liveness probing.

A single-shot ``probe_rag_health`` used by the settings route to validate a
server before insert/edit, surfacing the failure reason in the form. Lives
here (not in ``app/rag_servers.py``) to keep that module CRUD-only. Health
is a validate-on-write concern, not a render-time one.
"""

from urllib.parse import urlparse, urlunparse

import httpx

# Health endpoints are cheap (a status map, no FTS/ANN): 2s to connect, 5s total.
_HEALTH_TIMEOUT = httpx.Timeout(5.0, connect=2.0)
_HEALTHY_STATUS = "ok"


def _health_url(base_url: str) -> str | None:
    """Derive the ``/health`` URL from a typed RAG server base URL.

    Strips path/query/fragment and appends ``/health``. Returns ``None``
    if the URL is missing scheme or host.

    Args:
        base_url: Full RAG base URL as typed into the form.

    Returns:
        The ``/health`` URL, or ``None`` if ``base_url`` is malformed.
    """
    parsed = urlparse(base_url.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, "/health", "", "", ""))


async def probe_rag_health(name: str, base_url: str) -> tuple[bool, str]:
    """Probe ``/health`` for a named database; return ``(healthy, reason)``.

    Never raises: ``(True, "")`` on success, ``(False, <user-facing
    reason>)`` otherwise.

    HTTP status alone is NOT the verdict: the shared /health endpoint
    returns 503 when ANY hosted database is unhealthy, so we read the
    per-database map regardless of status and judge only the typed ``name``.

    Args:
        name: Database name to look up in the /health ``databases`` map.
        base_url: Full RAG base URL as typed (e.g. ``http://host1:8002/arxiv``).

    Returns:
        ``(healthy, reason)``; ``reason`` is empty on success.
    """
    health_url = _health_url(base_url)
    if health_url is None:
        return (
            False,
            "URL must include scheme and host"
            " (e.g. http://host1:8002/arxiv_rag).",
        )

    try:
        async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT) as client:
            response = await client.get(health_url)
    except httpx.HTTPError:
        return (
            False,
            f"Health check failed: server unreachable at {health_url}.",
        )

    try:
        body = response.json()
    except ValueError:
        body = None

    databases = body.get("databases") if isinstance(body, dict) else None
    if not isinstance(databases, dict):
        if response.status_code >= 400:
            return (
                False,
                f"Health check failed: HTTP {response.status_code} from {health_url}.",
            )
        if body is None:
            return (
                False,
                f"Health check failed: non-JSON response from {health_url}.",
            )
        return (
            False,
            f"Health check failed: /health response missing 'databases' map.",
        )

    if name not in databases:
        available = ", ".join(sorted(databases)) or "(none)"
        return (
            False,
            f"'{name}' not found in /health response."
            f" Available databases: {available}.",
        )

    reported = databases[name]
    if reported != _HEALTHY_STATUS:
        return (
            False,
            f"'{name}' is not healthy (status: {reported!r}).",
        )

    return (True, "")
