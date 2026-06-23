"""Configuration accessors backed by `.env`.

Machine-specific values (Ollama host, database path, ...) live in `.env`
at the project root. Importing this module calls `load_dotenv()` once;
the accessors read `os.environ` per call, so tests that monkeypatch the
env see the change without an import-time freeze.

Required accessors raise `KeyError` if unset — no in-code fallbacks. Run
`cp .env.example .env` before first use.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# Populate os.environ from .env. `load_dotenv()` does not override existing
# env vars, so a shell `export` or `monkeypatch.setenv` always wins — what
# we want for tests.
load_dotenv()


def ollama_host() -> str:
    """Return the configured Ollama base URL (`OLLAMA_HOST`).

    Raises:
        KeyError: If `OLLAMA_HOST` is not set.
    """
    return os.environ["OLLAMA_HOST"]


def db_path() -> Path:
    """Return the SQLite database path (`DB_PATH`), with `~` expanded.

    The parent directory is not created here — that is
    `initialize_database`'s job.

    Raises:
        KeyError: If `DB_PATH` is not set.
    """
    return Path(os.environ["DB_PATH"]).expanduser()


def file_tool_root() -> Path | None:
    """Return the workspace root the file tools are confined to, or None.

    The file tools resolve every path relative to this directory and reject
    anything that escapes it. A missing value is not an error: when
    `FILE_TOOL_ROOT` is unset the file tools are dropped from the registry
    entirely (see `app.tools.builtins.refresh_file_tools_registration`).

    `resolve()` collapses symlinks and `..` so the tools' containment check
    compares two fully-resolved paths.

    Returns:
        Absolute, resolved workspace root, or None when `FILE_TOOL_ROOT` is
        unset or empty.
    """
    raw = os.environ.get("FILE_TOOL_ROOT")
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def github_token() -> str | None:
    """Return the GitHub access token (`GITHUB_TOKEN`), or None if unset.

    Used by `fetch_github_file`. Optional: unset still works for public
    repos at the 60 req/hr unauthenticated limit; set unlocks private repos
    and the 5k req/hr quota.
    """
    raw = os.environ.get("GITHUB_TOKEN")
    return raw or None


def searxng_url() -> str | None:
    """Return the self-hosted SearXNG base URL (`SEARXNG_URL`), or None.

    Used by the `web_search` tool. Optional: when unset, `web_search` is
    dropped from the registry (see `refresh_web_search_registration`) so the
    model is never offered a search tool that cannot succeed — same gating
    as the file tools (`FILE_TOOL_ROOT`) and `query_rag` (no RAG servers).
    """
    raw = os.environ.get("SEARXNG_URL")
    return raw or None


def remote_ollama_host() -> str | None:
    """Return the remote Ollama base URL (`SLOWLY_OLLAMA_HOST`), or None.

    Optional second host. Paired with :func:`remote_ollama_model`: both
    must be set or the remote agent is dropped from the registry (no
    degenerate fallback).
    """
    raw = os.environ.get("SLOWLY_OLLAMA_HOST")
    return raw or None


def remote_ollama_model() -> str | None:
    """Return the remote host's model tag (`SLOWLY_OLLAMA_MODEL`), or None.

    Paired with :func:`remote_ollama_host`. The agent pins this tag and
    never falls back to a local model, so a missing remote model fails
    loudly rather than silently routing local.
    """
    raw = os.environ.get("SLOWLY_OLLAMA_MODEL")
    return raw or None


def extra_ollama_hosts() -> list[dict[str, str]]:
    """Return the non-primary Ollama hosts (the host picker's options).

    The primary host (`OLLAMA_HOST`) is excluded — it is the picker's
    leading "no selection" option. Each entry here is an additional machine
    a chat can be routed to.

    Two sources, in priority order:

    1. `OLLAMA_EXTRA_HOSTS` — a JSON array of objects with `name`, `url`,
       and `default_model` (`label` optional, defaults to `name`). Malformed
       JSON, a non-list top level, or an entry missing a required key is
       skipped defensively, so one typo can't take down the whole picker.
    2. Legacy fallback — when `OLLAMA_EXTRA_HOSTS` is unset, a single
       `host2` is synthesised from `SLOWLY_OLLAMA_HOST` +
       `SLOWLY_OLLAMA_MODEL` (both required), keeping pre-JSON deployments
       working.

    Returns:
        A list of `{"name", "label", "url", "default_model"}` dicts in
        declaration order. Empty when nothing is configured.
    """
    raw = os.environ.get("OLLAMA_EXTRA_HOSTS")
    if raw and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # Disable the extra hosts rather than crash every render that
            # touches the registry.
            return []
        hosts: list[dict[str, str]] = []
        if isinstance(parsed, list):
            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                url = entry.get("url")
                default_model = entry.get("default_model")
                # All three required; skip a half-configured entry rather
                # than register a host that fails on its first turn.
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
    """Return the rsync destination for the database, or None.

    An rsync/ssh remote spec (e.g. `"host:/path/to/slollillama_chats"`)
    naming the directory the backup module drops `chats.db` into. Paired
    with :func:`remote_workspace_path` via :func:`backups_enabled`.

    Returns:
        The remote spec, or None when `REMOTE_DB_PATH` is unset or empty.
    """
    raw = os.environ.get("REMOTE_DB_PATH")
    return raw or None


def remote_workspace_path() -> str | None:
    """Return the rsync destination for the workspaces, or None.

    An rsync/ssh remote spec (e.g. `"host:/path/to/agent_workspaces"`) the
    workspace tree (`FILE_TOOL_ROOT`) is pushed to on backup. Reads the
    existing `REMOTE_PATH` var — the same one `copy_agent_workspace.py`
    uses — so one setting drives both the script and the backup module.

    Returns:
        The remote spec, or None when `REMOTE_PATH` is unset or empty.
    """
    raw = os.environ.get("REMOTE_PATH")
    return raw or None


def backups_enabled() -> bool:
    """Return whether automatic remote backups are configured.

    Backups push the database and the workspaces to two separate
    destinations, so both :func:`remote_db_path` and
    :func:`remote_workspace_path` must be set. When either is missing the
    backup scheduler no-ops — no partial backups.
    """
    return remote_db_path() is not None and remote_workspace_path() is not None
