"""The ``fetch_github_file`` tool: pull a single file from a GitHub URL.

Accepts a blob URL (the web-UI share link) or a raw URL
(``raw.githubusercontent.com``). Blob URLs are normalized to raw form so
we always hit one host, sidestepping the Contents API's base64 envelope.

When ``GITHUB_TOKEN`` is set in ``.env``, it rides along in the
``Authorization`` header, enabling private-repo reads and the
authenticated rate limit (5k/hr vs. 60/hr). Public fetches work without
it, so the tool registers either way.

Imported at app startup (via ``app.routes``) so the ``@tool`` decorator
registers ``fetch_github_file`` before any code reads the registry.
"""

import re

import httpx

from app.config import github_token
from app.tools import tool

# Cap raw response so a large file can't blow the model's context. 100k
# is generous for source files (read_file caps at 50k).
_FETCH_CAP = 100_000

# 30s total / 5s connect, same as RAG retrieval: fail fast on a network
# blip rather than stall on the (usually fast) raw CDN.
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

# https://github.com/{owner}/{repo}/blob/{ref}/{path}
# {ref} is captured as one segment, so refs containing "/" (e.g.
# "feature/foo") aren't supported. GitHub's web UI escapes those.
_BLOB_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$"
)
# https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}
_RAW_RE = re.compile(
    r"^https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$"
)


def _to_raw_url(url: str) -> str | None:
    """Normalize a GitHub URL to its raw.githubusercontent.com form.

    Args:
        url: The user- or model-supplied URL.

    Returns:
        A ``https://raw.githubusercontent.com/...`` URL, or ``None`` if the
        input matches neither the blob nor raw pattern. The caller turns
        ``None`` into a model-facing error so the LLM reformats rather than
        retries blindly.
    """
    m = _BLOB_RE.match(url)
    if m:
        owner, repo, ref, path = m.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    if _RAW_RE.match(url):
        return url
    return None


@tool
async def fetch_github_file(url: str) -> str:
    """Fetch a single file's contents from a GitHub URL. Use this when the user shares a github.com link, or asks about specific code in a public repository — paste the blob or raw URL.

    Args:
        url: A GitHub file URL. Either a blob URL like
            ``https://github.com/{owner}/{repo}/blob/{ref}/{path}`` (the
            link you get from the web UI) or a raw URL like
            ``https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}``.
            ``{ref}`` may be a branch, tag, or commit SHA.
    """
    raw_url = _to_raw_url(url)
    if raw_url is None:
        return (
            f"Not a recognized GitHub file URL: '{url}'."
            " Expected https://github.com/owner/repo/blob/ref/path"
            " or https://raw.githubusercontent.com/owner/repo/ref/path."
        )

    headers: dict[str, str] = {}
    token = github_token()
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(raw_url, headers=headers)
    except httpx.HTTPError as e:
        return f"GitHub fetch failed: {e}"

    if response.status_code == 404:
        return f"File not found at {url}."
    if response.status_code == 403:
        # GitHub signals rate-limit exhaustion with 403 + this header.
        # Distinguish it so the model suggests GITHUB_TOKEN, not a retry.
        if response.headers.get("X-RateLimit-Remaining") == "0":
            hint = (
                " Set GITHUB_TOKEN in .env to raise the limit to 5000/hr."
                if token is None
                else ""
            )
            return f"GitHub rate limit exhausted.{hint}"
        return f"GitHub denied the request (HTTP 403) for {url}."
    if response.status_code >= 400:
        return (
            f"GitHub returned HTTP {response.status_code} for {url}."
        )

    try:
        text = response.text
    except UnicodeDecodeError:
        return f"File at {url} is not valid UTF-8 text."

    if len(text) > _FETCH_CAP:
        kb = len(text) // 1024
        text = (
            text[:_FETCH_CAP]
            + f"\n\n[truncated: file is {kb} KB, showing first {_FETCH_CAP // 1024} KB]"
        )
    return text
