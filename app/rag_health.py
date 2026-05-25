"""RAG server liveness probing with a TTL cache.

The single-shot probe (``probe_rag_health``) moved here verbatim from
``app/rag_servers.py`` so that module stays CRUD-only. New here: an in-memory
TTL cache keyed by base URL, plus a parallel orchestrator
(``get_health_map``) that the sidebar render and the on-send refresh both use.

Cache shape:
    ``{ base_url: _CacheEntry(healthy: bool, expires_at: float) }``

``expires_at`` is a ``time.monotonic()`` deadline so wall-clock skew doesn't
poison entries. Lifetime: ``HEALTH_TTL_SECONDS`` (60s).

Process-local, not on ``app.state`` — same pattern as
``app/generation.py``'s ``live_generations`` dict (one piece of cross-request
in-memory state per concern). Tests clear via ``clear_cache``.
"""

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import httpx

# Health endpoints are cheap (a status map, no FTS/ANN); two seconds to
# connect, five total. Same values as the original implementation.
_HEALTH_TIMEOUT = httpx.Timeout(5.0, connect=2.0)
_HEALTHY_STATUS = "ok"

HEALTH_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class _CacheEntry:
    """One cache row: probe result + monotonic-clock expiry."""

    healthy: bool
    expires_at: float


# Module-level cache. Key = base URL (NOT name — multiple chats may share a
# server, and a single server name maps to exactly one URL).
_cache: dict[str, _CacheEntry] = {}


def _now() -> float:
    """Wrapped so tests can monkeypatch the clock without touching time itself."""
    return time.monotonic()


def clear_cache() -> None:
    """Drop every cached entry. Called by tests via autouse fixture."""
    _cache.clear()


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
        base_url: Full RAG base URL as typed (e.g. ``http://pop-os:8002/arxiv``).

    Returns:
        Tuple of ``(healthy, reason)``. ``reason`` is empty on success.
    """
    health_url = _health_url(base_url)
    if health_url is None:
        return (
            False,
            "URL must include scheme and host"
            " (e.g. http://pop-os:8002/arxiv_rag).",
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


async def get_health(
    name: str, base_url: str, *, force: bool = False
) -> bool | None:
    """Return cached or freshly-probed health for one server.

    On cache miss (or ``force=True``) probes ``/health`` and caches the
    boolean result for ``HEALTH_TTL_SECONDS``. The reason string is
    intentionally discarded — sidebar chips only need on/off/unknown.

    Args:
        name: Database name as registered in ``rag_servers.name``.
        base_url: Full RAG base URL (the value stored in ``rag_servers.url``).
        force: When True, bypass the cache and always probe. Used by the
            on-send refresh path.

    Returns:
        ``True`` if healthy, ``False`` if known-unhealthy, ``None`` if the
        URL is malformed (chip renders as unknown / grey, never red —
        don't blame the user's typing).
    """
    if _health_url(base_url) is None:
        return None
    if not force:
        entry = _cache.get(base_url)
        if entry is not None and entry.expires_at > _now():
            return entry.healthy
    healthy, _reason = await probe_rag_health(name, base_url)
    _cache[base_url] = _CacheEntry(
        healthy=healthy,
        expires_at=_now() + HEALTH_TTL_SECONDS,
    )
    return healthy


async def get_health_map(
    servers: list,
    *,
    force: bool = False,
) -> dict[str, bool | None]:
    """Probe every server in parallel; return ``{server_name: status}``.

    Cache hits return synchronously; misses dispatch in one
    ``asyncio.gather`` so render-time latency is bounded by the slowest
    miss (5s tops). The sidebar render calls this once per chat-panel
    render.

    Args:
        servers: Iterable of objects with ``.name`` and ``.url`` attributes
            (typically ``list[RagServer]``; loose typing avoids an import
            cycle with ``app.rag_servers``).
        force: When True, every probe bypasses the cache.

    Returns:
        Mapping of server name → ``True``/``False``/``None`` per
        ``get_health``.
    """
    async def _one(server) -> tuple[str, bool | None]:
        return server.name, await get_health(server.name, server.url, force=force)

    results = await asyncio.gather(*(_one(s) for s in servers))
    return dict(results)
