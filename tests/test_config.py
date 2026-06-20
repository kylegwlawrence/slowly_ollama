"""Tests for app.config: the .env-backed configuration accessors.

These verify the contract: accessors read from os.environ at call time
(not at import time) and raise KeyError when keys are missing.
"""

import json
from pathlib import Path

import pytest

from app.config import db_path, extra_ollama_hosts, ollama_host


def test_ollama_host_returns_env_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ollama_host() returns the current OLLAMA_HOST value."""
    monkeypatch.setenv("OLLAMA_HOST", "http://example.com:9999")
    assert ollama_host() == "http://example.com:9999"


def test_ollama_host_raises_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ollama_host() raises KeyError when OLLAMA_HOST is missing.

    No in-code fallback by design; .env (or the process env) must
    supply the value.
    """
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    with pytest.raises(KeyError):
        ollama_host()


def test_db_path_expands_tilde(monkeypatch: pytest.MonkeyPatch) -> None:
    """db_path() expands `~` so the same .env value works per-user."""
    monkeypatch.setenv("DB_PATH", "~/some/where/chats.db")
    result = db_path()
    # After expansion, the path must be absolute and rooted at the
    # current user's home — never start with a literal `~`.
    assert "~" not in str(result)
    assert result.is_absolute()
    assert str(result).startswith(str(Path.home()))


def test_db_path_returns_path_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """db_path() returns a pathlib.Path, not a string."""
    monkeypatch.setenv("DB_PATH", "/tmp/chats.db")
    assert isinstance(db_path(), Path)


def test_db_path_raises_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """db_path() raises KeyError when DB_PATH is missing."""
    monkeypatch.delenv("DB_PATH", raising=False)
    with pytest.raises(KeyError):
        db_path()


# ---------------------------------------------------------------------------
# extra_ollama_hosts: the configurable N-machine list
# ---------------------------------------------------------------------------


def _clear_host_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every host-config env var so a test starts from a clean slate."""
    for key in (
        "OLLAMA_EXTRA_HOSTS",
        "SLOWLY_OLLAMA_HOST",
        "SLOWLY_OLLAMA_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_extra_ollama_hosts_empty_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No host config → empty list (the picker shows only the primary host)."""
    _clear_host_env(monkeypatch)
    assert extra_ollama_hosts() == []


def test_extra_ollama_hosts_parses_json_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OLLAMA_EXTRA_HOSTS JSON → one dict per machine, label defaults to name."""
    _clear_host_env(monkeypatch)
    monkeypatch.setenv(
        "OLLAMA_EXTRA_HOSTS",
        json.dumps(
            [
                {
                    "name": "host2",
                    "url": "http://host2:11434",
                    "default_model": "qwen2.5:14b",
                },
                {
                    "name": "mac",
                    "label": "Mac Studio",
                    "url": "http://mac:11434",
                    "default_model": "llama3:70b",
                },
            ]
        ),
    )
    hosts = extra_ollama_hosts()
    assert [h["name"] for h in hosts] == ["host2", "mac"]
    assert hosts[0]["label"] == "host2"  # defaults to name
    assert hosts[0]["default_model"] == "qwen2.5:14b"
    assert hosts[1]["label"] == "Mac Studio"
    assert hosts[1]["url"] == "http://mac:11434"


def test_extra_ollama_hosts_skips_incomplete_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry missing a required key is dropped, not fatal to the rest."""
    _clear_host_env(monkeypatch)
    monkeypatch.setenv(
        "OLLAMA_EXTRA_HOSTS",
        json.dumps(
            [
                {"name": "ok", "url": "http://ok:11434", "default_model": "m"},
                {"name": "missing-url", "default_model": "m"},
                {"url": "http://no-name:11434", "default_model": "m"},
                # A non-dict element (e.g. a stray string) is skipped, not fatal.
                "not-a-dict",
            ]
        ),
    )
    assert [h["name"] for h in extra_ollama_hosts()] == ["ok"]


def test_extra_ollama_hosts_malformed_json_disables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed JSON yields an empty list rather than raising."""
    _clear_host_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_EXTRA_HOSTS", "{not json]")
    assert extra_ollama_hosts() == []


def test_extra_ollama_hosts_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With OLLAMA_EXTRA_HOSTS unset, the legacy SLOWLY_* pair → one host."""
    _clear_host_env(monkeypatch)
    monkeypatch.setenv("SLOWLY_OLLAMA_HOST", "http://host1:11434")
    monkeypatch.setenv("SLOWLY_OLLAMA_MODEL", "llama3.1:70b")
    hosts = extra_ollama_hosts()
    assert hosts == [
        {
            "name": "host2",
            "label": "host2",
            "url": "http://host1:11434",
            "default_model": "llama3.1:70b",
        }
    ]


def test_extra_ollama_hosts_legacy_partial_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial legacy config (host but no model) → empty (both-or-nothing)."""
    _clear_host_env(monkeypatch)
    monkeypatch.setenv("SLOWLY_OLLAMA_HOST", "http://host1:11434")
    assert extra_ollama_hosts() == []
