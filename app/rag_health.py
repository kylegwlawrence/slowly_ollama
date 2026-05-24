"""Phase 12e: liveness probe for the remote RAG server.

The remote RAG host (e.g. ``http://host1:8002``) exposes a single
``/health`` endpoint that reports the status of every database it
hosts as a flat ``{name: status}`` map under ``databases``:

.. code-block:: json

    {
      "ok": true,
      "databases": {
        "arxiv_rag": "ok",
        "factbook_rag": "ok",
        ...
      }
    }

Before inserting a new ``rag_servers`` row we synchronously call this
endpoint and verify that the user-typed name appears as a key AND
reports ``"ok"``. The route at ``POST /settings/servers`` short-
circuits with a 502 on any failure, surfacing the reason in the
add-server form's existing inline error region.

The /health URL is derived from the typed base URL: we keep the
``scheme://host:port`` portion and replace the rest with ``/health``.
That way a single shared endpoint serves any number of source-prefixed
URLs on the same host.

The name is accepted as-is: the remote's queryable ``/chunks``
endpoints live at the plain database names (e.g. ``arxiv``,
``pydocs``), so we do NOT require any particular naming suffix — we
only check that the typed name is present and healthy.
"""

import httpx
from urllib.parse import urlparse, urlunparse

# Health endpoints are cheap (no FTS/ANN, just a status map), so we
# pull the timeout in tight relative to ``query_rag``'s 30s. Two
# seconds to connect handles a slow LAN/VPN hop; five seconds
# total fails fast on a hung server without making the user wait.
_HEALTH_TIMEOUT = httpx.Timeout(5.0, connect=2.0)

# What the /health endpoint returns for a healthy database. Anything
# else (including missing keys) is treated as unhealthy.
_HEALTHY_STATUS = "ok"


def _health_url(base_url: str) -> str | None:
    """Derive the ``/health`` URL from a typed RAG server base URL.

    Strips path/query/fragment and replaces them with ``/health`` so a
    URL like ``http://host1:8002/arxiv_rag`` becomes
    ``http://host1:8002/health``. Returns ``None`` if the parsed URL
    is missing scheme or host — caller should treat that as a bad URL.

    Args:
        base_url: The full RAG base URL as typed into the form.

    Returns:
        The ``/health`` URL, or ``None`` if ``base_url`` is malformed.
    """
    parsed = urlparse(base_url.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, "/health", "", "", ""))


async def probe_rag_health(name: str, base_url: str) -> tuple[bool, str]:
    """Probe ``/health`` for a named database; return (healthy, reason).

    On success returns ``(True, "")``. On any failure returns
    ``(False, <user-facing reason>)`` — a plain string the route
    handler can stuff verbatim into the form's error region. We never
    raise; callers always get a tuple back.

    A non-2xx status alone is NOT treated as failure: the shared
    /health endpoint returns 503 whenever ANY hosted database is
    unhealthy, so we read the per-database map regardless of status and
    judge only the specific ``name`` requested. The HTTP status is used
    as the failure reason only when the body isn't a usable map.

    Failure cases:
        * Malformed URL (no scheme or host).
        * Network error (DNS, connect, timeout, read).
        * Error HTTP status AND an unparseable / map-less body.
        * Response body isn't JSON / lacks a ``databases`` map.
        * ``name`` isn't a key under ``databases``.
        * ``name`` is present but its status isn't ``"ok"``.

    Args:
        name: The database name the user typed into the form. Must
            match a key under the /health response's ``databases``
            map exactly (e.g. ``arxiv`` or ``pydocs``).
        base_url: The full RAG base URL as typed (e.g.
            ``http://host1:8002/arxiv``). The /health URL is
            derived from this.

    Returns:
        Tuple of (healthy, reason). ``reason`` is empty on success.
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
        # DNS, connect refused, timeout, read error — all surface as
        # the same user-facing "unreachable" message. The health URL
        # itself is echoed so the user can sanity-check what we
        # actually hit (vs. what they typed).
        return (
            False,
            f"Health check failed: server unreachable at {health_url}.",
        )

    # Parse the body BEFORE gating on the HTTP status. A single
    # unhealthy database makes the shared /health endpoint report both
    # ``"ok": false`` AND an HTTP 503, even though the per-database map
    # still reports every *other* database correctly. The status of the
    # SPECIFIC database the user typed (read from the map below) is what
    # matters — one broken sibling on the same host must not block
    # adding a healthy database. We only fall back to the HTTP status as
    # a failure reason when the body isn't a usable databases map.
    try:
        body = response.json()
    except ValueError:
        body = None

    databases = body.get("databases") if isinstance(body, dict) else None
    if not isinstance(databases, dict):
        # Body wasn't a usable {"databases": {...}} map. Prefer the HTTP
        # status as the reason when it was an error code (more actionable
        # than "missing map"); otherwise describe the malformed body.
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
        # List the databases the remote currently reports so the user
        # can correct a typo against the live set.
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
