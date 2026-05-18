"""Tests for app.config: the .env-backed configuration accessors.

These verify the contract: accessors read from os.environ at call time
(not at import time) and raise KeyError when keys are missing.
"""

from pathlib import Path

import pytest

from app.config import db_path, ollama_host


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
