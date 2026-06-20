"""Configuration loaded from `.env`.

All "fairly static" values that might differ between machines (Ollama
host, database path) live in `.env` at the project root. Importing this
module calls `load_dotenv()` so subsequent `os.environ` reads see those
values; the accessors below run per call, so tests that monkeypatch the
env (or delete keys) see the change without an import-time freeze.

Accessors raise `KeyError` if the key isn't set — there are no in-code
fallbacks. The setup ritual is `cp .env.example .env` before first run.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# Side-effect import: populate os.environ from .env at the project root.
# `load_dotenv()` walks up from CWD looking for a .env file. By default
# it does NOT override existing env vars, which means a shell `export`
# or pytest's `monkeypatch.setenv` always wins over the file — exactly
# what we want for tests.
load_dotenv()


def ollama_host() -> str:
    """Return the configured Ollama base URL.

    Returns:
        Value of the `OLLAMA_HOST` env var (set via `.env` or the
        process environment).

    Raises:
        KeyError: If `OLLAMA_HOST` is not set anywhere.
    """
    return os.environ["OLLAMA_HOST"]


def db_path() -> Path:
    """Return the configured SQLite database path with `~` expanded.

    `expanduser()` lets the same `.env` value (e.g.
    `~/Library/Application Support/...`) work on any user's machine —
    the path resolves to the current user's home at read time.

    Returns:
        Absolute `Path` to the database file. The parent directory is
        not created here; that is `initialize_database`'s job.

    Raises:
        KeyError: If `DB_PATH` is not set.
    """
    return Path(os.environ["DB_PATH"]).expanduser()


def file_tool_root() -> Path | None:
    """Return the workspace directory the file tools are confined to, or None.

    The ``read_file`` / ``write_file`` tools resolve every path relative
    to this directory and reject anything that escapes it. Unlike
    :func:`ollama_host` and :func:`db_path`, a missing value is NOT an
    error: when ``FILE_TOOL_ROOT`` is unset the file tools are removed
    from the registry entirely (see
    ``app.tools.builtins.refresh_file_tools_registration``), so the chat
    model is never offered a tool with nowhere to operate.

    ``expanduser()`` lets a ``~``-prefixed value resolve to the current
    user's home; ``resolve()`` collapses symlinks and ``..`` so the
    sandbox containment check inside the tools compares two
    fully-resolved paths.

    Returns:
        Absolute, resolved ``Path`` to the workspace root, or ``None``
        when ``FILE_TOOL_ROOT`` is unset (or set to an empty string).
    """
    raw = os.environ.get("FILE_TOOL_ROOT")
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def github_token() -> str | None:
    """Return the GitHub personal access token, or ``None`` if unset.

    Used by ``fetch_github_file`` to authenticate raw.githubusercontent.com
    requests. Optional: when unset the tool still works for public repos
    (subject to GitHub's 60 req/hr unauthenticated rate limit); when set
    it unlocks private repos and the 5k req/hr authenticated quota.

    Returns:
        The token string, or ``None`` when ``GITHUB_TOKEN`` is unset or
        empty.
    """
    raw = os.environ.get("GITHUB_TOKEN")
    return raw or None


def remote_ollama_host() -> str | None:
    """Return the base URL of the remote Ollama instance, or ``None`` if unset.

    Optional second Ollama host (e.g. a VPN-reachable machine).
    Paired with :func:`remote_ollama_model`: both must be set for the
    remote agent to register — when either is missing the agent is
    dropped from the registry rather than registered with a degenerate
    fallback, matching the gating pattern used by ``query_rag`` and the
    file tools.

    Returns:
        The URL string (e.g. ``"http://host1:11434"``) or ``None`` when
        ``SLOWLY_OLLAMA_HOST`` is unset or empty.
    """
    raw = os.environ.get("SLOWLY_OLLAMA_HOST")
    return raw or None


def remote_ollama_model() -> str | None:
    """Return the Ollama model tag installed on the remote host, or ``None``.

    Paired with :func:`remote_ollama_host`. The agent pins this tag and
    never falls back to a local model — if the remote model isn't
    installed on the remote host, the agent's first turn fails loudly
    rather than silently routing to the local Ollama.

    Returns:
        The model tag (e.g. ``"llama3.1:70b"``) or ``None`` when
        ``SLOWLY_OLLAMA_MODEL`` is unset or empty.
    """
    raw = os.environ.get("SLOWLY_OLLAMA_MODEL")
    return raw or None


def extra_ollama_hosts() -> list[dict[str, str]]:
    """Return the configured non-primary Ollama hosts (the host picker's options).

    The primary host (``OLLAMA_HOST``) is NOT in this list — it is the picker's
    leading "no selection" option (``active_agent`` NULL). Each entry here is an
    *additional* machine a chat can be routed to, so the user can add machines
    without a code change.

    Two sources, in priority order:

    1. ``OLLAMA_EXTRA_HOSTS`` — a JSON array of objects, each with ``name``,
       ``url``, and ``default_model`` (``label`` optional, defaults to
       ``name``). This is the scalable, N-machine config: add a machine by
       appending an object. Malformed JSON, a non-list top level, or an entry
       missing a required key is skipped defensively — a typo in one machine
       must not take the whole picker down.
    2. Legacy fallback — when ``OLLAMA_EXTRA_HOSTS`` is unset/empty, a single
       ``host2`` host is synthesised from ``SLOWLY_OLLAMA_HOST`` +
       ``SLOWLY_OLLAMA_MODEL`` (both required), so deployments predating the
       JSON config keep working without an .env change.

    Returns:
        A list of ``{"name", "label", "url", "default_model"}`` dicts (all
        strings), in declaration order. Empty when nothing is configured.
    """
    raw = os.environ.get("OLLAMA_EXTRA_HOSTS")
    if raw and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # A malformed value disables the extra hosts rather than crashing
            # every render that touches the registry.
            return []
        hosts: list[dict[str, str]] = []
        if isinstance(parsed, list):
            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                url = entry.get("url")
                default_model = entry.get("default_model")
                # All three are required; skip a half-configured entry rather
                # than register a host that would fail on its first turn (the
                # same both-or-nothing gating used by the legacy pair below).
                if not (name and url and default_model):
                    continue
                hosts.append(
                    {
                        "name": str(name),
                        "label": str(entry.get("label") or name),
                        "url": str(url),
                        "default_model": str(default_model),
                    }
                )
        return hosts
    # Legacy single-host fallback (pre-OLLAMA_EXTRA_HOSTS deployments).
    host = remote_ollama_host()
    model = remote_ollama_model()
    if host and model:
        return [
            {
                "name": "host2",
                "label": "host2",
                "url": host,
                "default_model": model,
            }
        ]
    return []


def remote_db_path() -> str | None:
    """Return the ``host:/dir`` rsync destination for the database, or ``None``.

    The SQLite database is pushed (mirror-style, overwriting) to this
    location on every backup. The value is an rsync/ssh remote spec
    (e.g. ``"host:/path/to/olliellama_chats"``) naming the *directory*
    the backup module drops ``chats.db`` into.

    Paired with :func:`remote_workspace_path` via :func:`backups_enabled`:
    backups only run when both are set, following the no-degenerate-
    fallback gating used by the file tools and the remote Ollama agent.

    Returns:
        The remote spec, or ``None`` when ``REMOTE_DB_PATH`` is unset or
        empty.
    """
    raw = os.environ.get("REMOTE_DB_PATH")
    return raw or None


def remote_workspace_path() -> str | None:
    """Return the ``host:/dir`` rsync destination for the workspaces, or ``None``.

    The agent workspace tree (``FILE_TOOL_ROOT``) is pushed (mirror-style)
    to this location on every backup. The value is an rsync/ssh remote
    spec (e.g. ``"host:/path/to/agent_workspaces"``).

    Reads the existing ``REMOTE_PATH`` env var — the same default
    consumed by the standalone ``copy_agent_workspace.py`` script — so a
    single setting drives both the manual script and the automatic
    backup module.

    Returns:
        The remote spec, or ``None`` when ``REMOTE_PATH`` is unset or
        empty.
    """
    raw = os.environ.get("REMOTE_PATH")
    return raw or None


def backups_enabled() -> bool:
    """Return whether automatic remote backups are configured.

    Backups push two things — the database and the workspaces — to two
    separate remote destinations, so both :func:`remote_db_path` and
    :func:`remote_workspace_path` must be set for the feature to engage.
    When either is missing the backup scheduler no-ops (the same gating
    discipline as :func:`file_tool_root` and :func:`remote_ollama_host`):
    no partial backups, no surprises.

    Returns:
        ``True`` only when both remote destinations are configured.
    """
    return remote_db_path() is not None and remote_workspace_path() is not None
