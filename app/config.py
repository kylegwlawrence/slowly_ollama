"""Configuration loaded from `.env`.

All "fairly static" values that might differ between machines (Ollama
host, database path) live in `.env` at the project root. Importing this
module calls `load_dotenv()` so subsequent `os.environ` reads see those
values; the accessors below run per call, so tests that monkeypatch the
env (or delete keys) see the change without an import-time freeze.

Accessors raise `KeyError` if the key isn't set — there are no in-code
fallbacks. The setup ritual is `cp .env.example .env` before first run.
"""

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
