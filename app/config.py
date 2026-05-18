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
