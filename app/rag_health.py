"""RAG server liveness probing.

The single-shot probe (``probe_rag_health``) moved here from
``app/rag_servers.py`` so that module stays CRUD-only. The settings route uses
it to validate a server before insert/edit — it surfaces the failure reason in
the form so the user knows why a server was rejected.

Phase 24 removed the TTL cache + parallel orchestrator (``get_health_map``)
that only ever fed the sidebar's chat-gated Sources health panel; that panel was
replaced by an always-visible, health-free reference list. Health is now a
validate-on-write concern, not a render-time one.
"""

from urllib.parse import urlparse, urlunparse

import httpx

# Health endpoints are cheap (a status map, no FTS/ANN); two seconds to
# connect, five total. Same values as the original implementation.
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

    On success returns ``(True, "")``. On any failure returns
    ``(False, <user-facing reason>)``. Never raises.

    A non-2xx status alone is NOT treated as failure: the shared /health
    endpoint returns 503 when ANY hosted database is unhealthy, but the
    per-database map still reports each entry correctly. We read the map
    regardless of HTTP status and judge only the specific ``name`` the
    user typed.

    Args:
        name: Database name to look up under the /health ``databases`` map.
        base_url: Full RAG base URL as typed (e.g. ``http://host1:8002/arxiv``).

    Returns:
        Tuple of ``(healthy, reason)``. ``reason`` is empty on success.
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
