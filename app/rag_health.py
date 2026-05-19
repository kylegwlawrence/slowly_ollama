"""Phase 12e: liveness probe for the remote RAG server.

The remote RAG host (e.g. ``http://pop-os:8002``) exposes a single
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
"""

import httpx
from urllib.parse import urlparse, urlunparse

# Health endpoints are cheap (no FTS/ANN, just a status map), so we
# pull the timeout in tight relative to ``query_rag``'s 15s. Two
# seconds to connect handles a slow LAN/Tailscale hop; five seconds
# total fails fast on a hung server without making the user wait.
_HEALTH_TIMEOUT = httpx.Timeout(5.0, connect=2.0)

# What the /health endpoint returns for a healthy database. Anything
# else (including missing keys) is treated as unhealthy.
_HEALTHY_STATUS = "ok"

# RAG-flavoured databases on the remote host use this suffix to
# distinguish themselves from sibling non-RAG databases that share
# the same hostname (e.g. ``arxiv`` is a plain database; ``arxiv_rag``
# is its chunked/embedded counterpart). The ``query_rag`` tool only
# knows how to talk to the RAG variant, so we reject names that
# don't carry this suffix even when /health reports them healthy.
_RAG_SUFFIX = "_rag"


def _health_url(base_url: str) -> str | None:
    """Derive the ``/health`` URL from a typed RAG server base URL.

    Strips path/query/fragment and replaces them with ``/health`` so a
    URL like ``http://pop-os:8002/arxiv_rag`` becomes
    ``http://pop-os:8002/health``. Returns ``None`` if the parsed URL
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

    Failure cases:
        * Malformed URL (no scheme or host).
        * Network error (DNS, connect, timeout, read).
        * Non-2xx HTTP response from /health.
        * Response body isn't JSON / lacks a ``databases`` map.
        * ``name`` isn't a key under ``databases``.
        * ``name`` is present but its status isn't ``"ok"``.

    Args:
        name: The database name the user typed into the form. Must
            match a key under the /health response's ``databases``
            map exactly (e.g. ``arxiv_rag``, not ``arxiv``).
        base_url: The full RAG base URL as typed (e.g.
            ``http://pop-os:8002/arxiv_rag``). The /health URL is
            derived from this.

    Returns:
        Tuple of (healthy, reason). ``reason`` is empty on success.
    """
    health_url = _health_url(base_url)
    if health_url is None:
        return (
            False,
            "URL must include scheme and host"
            " (e.g. http://pop-os:8002/arxiv_rag).",
        )

    # Suffix check happens BEFORE the network call: a name like
    # 'arxiv' is healthy on the remote but isn't a RAG endpoint, so
    # the /chunks API our tool expects wouldn't be there. Fail fast
    # without a round-trip.
    if not name.endswith(_RAG_SUFFIX):
        return (
            False,
            f"Name must end in '{_RAG_SUFFIX}' to identify a RAG database"
            f" (e.g. arxiv{_RAG_SUFFIX}, factbook{_RAG_SUFFIX}).",
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

    if response.status_code >= 400:
        return (
            False,
            f"Health check failed: HTTP {response.status_code} from {health_url}.",
        )

    try:
        body = response.json()
    except ValueError:
        return (
            False,
            f"Health check failed: non-JSON response from {health_url}.",
        )

    databases = body.get("databases") if isinstance(body, dict) else None
    if not isinstance(databases, dict):
        return (
            False,
            f"Health check failed: /health response missing 'databases' map.",
        )

    if name not in databases:
        # Surface the actual _rag databases the remote currently
        # reports so the error stays accurate as the user adds more
        # ``*_rag`` sources on the RAG box.
        rag_names = sorted(
            key for key in databases if key.endswith(_RAG_SUFFIX)
        )
        available = ", ".join(rag_names) if rag_names else "(none)"
        return (
            False,
            f"'{name}' not found in /health response."
            f" Available RAG databases: {available}.",
        )

    reported = databases[name]
    if reported != _HEALTHY_STATUS:
        return (
            False,
            f"'{name}' is not healthy (status: {reported!r}).",
        )

    return (True, "")
