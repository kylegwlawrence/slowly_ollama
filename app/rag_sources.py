"""Fetch source metadata from a RAG host's ``/sources`` endpoint.

The RAG server application advertises its datasources at ``GET /sources``
(host root), returning ``id`` / ``name`` / ``description`` / ``timeframe`` /
``chunks_endpoint`` per source. The settings "Sync descriptions" action uses
this to overwrite each configured ``rag_servers`` row's description from the
canonical server-side copy, so descriptions stay common across apps without
manual typing.

Lives apart from :mod:`app.rag_servers` (CRUD-only) and :mod:`app.rag_health`
(liveness): this is a one-shot metadata fetch — a third, distinct network
concern.
"""

from urllib.parse import urlparse, urlunparse

import httpx

# /sources is a small static metadata list (no FTS/ANN work): 2s to connect,
# 5s total — same budget as the /health probe in app.rag_health.
_SOURCES_TIMEOUT = httpx.Timeout(5.0, connect=2.0)

# Server rows follow the naming convention ``<source-id>_rag`` (e.g.
# ``arxiv_rag`` for source id ``arxiv``). Stripping this suffix is how a
# configured row is matched back to a /sources entry — the stored URL path
# doesn't line up (``…/arxiv_rag`` vs canonical ``/arxiv/chunks``).
_NAME_SUFFIX = "_rag"

# Mirror app.rag_servers' description column cap so a synced value never
# exceeds what the table (and the inline editor) store.
_DESCRIPTION_CAP = 400


def host_root(base_url: str) -> str | None:
    """Return ``scheme://netloc`` for a configured server base URL.

    Used to de-duplicate ``/sources`` fetches: every server on the same host
    shares one source list, so the sync route fetches once per distinct root.

    Args:
        base_url: Full RAG server base URL as stored (e.g.
            ``http://host:8002/arxiv_rag``).

    Returns:
        The bare host root (e.g. ``http://host:8002``), or ``None`` when the
        URL is missing a scheme or host.
    """
    parsed = urlparse(base_url.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def _sources_url(base_url: str) -> str | None:
    """Derive the host-root ``/sources`` URL from a server base URL.

    Strips path/query/fragment and appends ``/sources`` — the same
    host-rooting :func:`app.rag_health._health_url` does for ``/health``.

    Args:
        base_url: Full RAG server base URL as stored.

    Returns:
        The ``/sources`` URL, or ``None`` when ``base_url`` is malformed.
    """
    parsed = urlparse(base_url.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, "/sources", "", "", ""))


async def fetch_sources(base_url: str) -> dict[str, dict] | None:
    """Fetch ``GET /sources`` for a host; return an ``id`` → entry map.

    Never raises. Returns ``None`` on a malformed URL, an unreachable host, a
    non-JSON body, or a payload missing the expected ``items`` list, so the
    sync route can mark every server on that host unmatched without aborting
    the whole pass.

    Args:
        base_url: Any configured server URL on the target host; only its
            scheme + host are used to build the ``/sources`` URL.

    Returns:
        A map from lowercased source ``id`` to its raw entry dict, or
        ``None`` when the endpoint can't be read.
    """
    url = _sources_url(base_url)
    if url is None:
        return None

    try:
        async with httpx.AsyncClient(timeout=_SOURCES_TIMEOUT) as client:
            response = await client.get(url)
    except httpx.HTTPError:
        return None

    try:
        body = response.json()
    except ValueError:
        return None

    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return None

    by_id: dict[str, dict] = {}
    for entry in items:
        if isinstance(entry, dict) and entry.get("id"):
            by_id[str(entry["id"]).strip().lower()] = entry
    return by_id


def _source_key(server_name: str) -> str:
    """Map a configured server name to its ``/sources`` ``id`` lookup key.

    Strips a trailing ``_rag`` and lowercases, per the naming convention
    (``arxiv_rag`` → ``arxiv``). A name that doesn't follow the convention
    matches on its full lowercased form.

    Args:
        server_name: The ``rag_servers.name`` value.

    Returns:
        The lowercased source id to look up in a :func:`fetch_sources` map.
    """
    name = server_name.strip().lower()
    if name.endswith(_NAME_SUFFIX):
        name = name[: -len(_NAME_SUFFIX)]
    return name


def description_for(
    server_name: str, sources_by_id: dict[str, dict]
) -> str | None:
    """Build the synced description for a server, or ``None`` if unmatched.

    Looks up the ``/sources`` entry by the ``_rag``-stripped name, then
    formats ``"<description> (<timeframe>)"`` — omitting the parenthetical
    when ``timeframe`` is blank. Truncated to the description column's cap.

    Args:
        server_name: The ``rag_servers.name`` to match.
        sources_by_id: The ``id`` → entry map from :func:`fetch_sources`.

    Returns:
        The composed description, or ``None`` when no source matches the name
        or the matched entry carries no description text.
    """
    entry = sources_by_id.get(_source_key(server_name))
    if entry is None:
        return None

    description = str(entry.get("description") or "").strip()
    if not description:
        # An entry with no description gives us nothing worth writing; treat
        # it as unmatched rather than blanking a possibly-useful local value.
        return None

    timeframe = str(entry.get("timeframe") or "").strip()
    text = f"{description} ({timeframe})" if timeframe else description
    return text[:_DESCRIPTION_CAP]
