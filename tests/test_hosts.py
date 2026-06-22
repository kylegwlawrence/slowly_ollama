"""Tests for the Ollama-host registry (formerly the agent registry).

The picker selects an Ollama host: the primary host (``OLLAMA_HOST``) is the
absence of a selection, and an optional "host2" second host
(``SLOWLY_OLLAMA_HOST``) is registered when its env vars are set. The old
persona agents (Research, Content Generator) were removed, and Phase 23 renamed
the module/classes from ``agent`` to ``host``.
"""

from pathlib import Path

import pytest

from app import hosts, queries
from app.hosts import (
    HOSTS,
    HostSpec,
    enabled_hosts,
    get_host,
    host_label,
    list_hosts,
)
from app.connection import open_connection
from app.db import initialize_database


@pytest.fixture
def _db(tmp_path: Path):
    """Open a fresh SQLite connection with the schema initialized.

    Used by phase-20b tests that need the ``app_settings`` table to
    drive the ``enabled_hosts`` / ``_resolve_active_host`` toggle.
    """
    db_path = tmp_path / "chats.db"
    initialize_database(db_path)
    with open_connection(db_path) as conn:
        yield conn


def _slowly_spec() -> HostSpec:
    """A stand-in "host2" host spec for injection into HOSTS in tests."""
    return HostSpec(
        name="host2", label="host2", description="d",
        model="m", ollama_host="http://host1:11434",
    )


def test_registry_has_no_persona_agents() -> None:
    """The old persona agents are gone; every registered entry is a host.

    The primary host is implicit (not in HOSTS). Any entry present is a
    second host, identified by a non-None ``ollama_host``.
    """
    assert "research" not in HOSTS
    assert "content_generator" not in HOSTS
    for spec in HOSTS.values():
        assert isinstance(spec, HostSpec)
        assert spec.ollama_host is not None  # a host, not a persona agent


def test_host_specs_are_populated() -> None:
    """Any registered host has the fields a host needs (label, model, host URL)."""
    for spec in HOSTS.values():
        assert spec.name and isinstance(spec.name, str)
        assert spec.label and isinstance(spec.label, str)
        assert spec.model and isinstance(spec.model, str)
        assert spec.ollama_host and isinstance(spec.ollama_host, str)


def test_get_host_resolves_names_and_primary() -> None:
    """get_host maps a known host name to its spec; None/""/unknown → None.

    None (the primary host) and any unknown/removed name both resolve to None
    so the generation layer falls back to plain chat on the primary host.
    """
    assert get_host(None) is None
    assert get_host("") is None
    assert get_host("does_not_exist") is None
    assert get_host("research") is None  # removed persona agent

    saved = HOSTS.get("host2")
    HOSTS["host2"] = _slowly_spec()
    try:
        assert get_host("host2") is HOSTS["host2"]
    finally:
        if saved is None:
            HOSTS.pop("host2", None)
        else:
            HOSTS["host2"] = saved


def test_list_hosts_returns_registry_in_order() -> None:
    """list_hosts preserves insertion (dropdown) order."""
    assert list_hosts() == list(HOSTS.values())


def test_hostspec_is_frozen() -> None:
    """Specs are immutable so a stray mutation can't corrupt the registry."""
    import dataclasses

    spec = _slowly_spec()
    try:
        spec.model = "other"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("HostSpec should be frozen")


def test_hostspec_ollama_host_defaults_none_and_is_settable() -> None:
    """`ollama_host` defaults None (primary/local) and is opt-in per spec."""
    bare = HostSpec(name="x", label="X", description="d", model="m")
    assert bare.ollama_host is None

    remote = HostSpec(
        name="y", label="Y", description="d", model="m",
        ollama_host="http://host1:11434",
    )
    assert remote.ollama_host == "http://host1:11434"


def test_build_hosts_empty_when_no_hosts(monkeypatch) -> None:
    """No extra-host config → empty registry (matches tool-gating pattern)."""
    from app.hosts import _build_hosts

    monkeypatch.delenv("OLLAMA_EXTRA_HOSTS", raising=False)
    monkeypatch.delenv("SLOWLY_OLLAMA_HOST", raising=False)
    monkeypatch.delenv("SLOWLY_OLLAMA_MODEL", raising=False)
    assert _build_hosts() == {}


def test_build_hosts_legacy_slowly_fallback(monkeypatch) -> None:
    """With OLLAMA_EXTRA_HOSTS unset, the legacy SLOWLY_* pair → one host."""
    from app.hosts import _build_hosts

    monkeypatch.delenv("OLLAMA_EXTRA_HOSTS", raising=False)
    monkeypatch.setenv("SLOWLY_OLLAMA_HOST", "http://host1:11434")
    monkeypatch.setenv("SLOWLY_OLLAMA_MODEL", "llama3.1:70b")
    hosts_map = _build_hosts()
    assert set(hosts_map) == {"host2"}
    spec = hosts_map["host2"]
    assert spec.label == "host2"
    assert spec.model == "llama3.1:70b"
    assert spec.ollama_host == "http://host1:11434"


def test_build_hosts_drops_partial_legacy(monkeypatch) -> None:
    """Partial legacy config (host but no model) is treated as no config."""
    from app.hosts import _build_hosts

    monkeypatch.delenv("OLLAMA_EXTRA_HOSTS", raising=False)
    monkeypatch.setenv("SLOWLY_OLLAMA_HOST", "http://host1:11434")
    monkeypatch.delenv("SLOWLY_OLLAMA_MODEL", raising=False)
    assert _build_hosts() == {}


def test_build_hosts_from_extra_hosts_json(monkeypatch) -> None:
    """OLLAMA_EXTRA_HOSTS JSON builds one host spec per entry, in order.

    ``label`` defaults to ``name`` when omitted; ``default_model`` maps to the
    spec's ``model`` (the host's fallback model).
    """
    import json

    from app.hosts import _build_hosts

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
    hosts_map = _build_hosts()
    assert list(hosts_map) == ["host2", "mac"]
    assert hosts_map["host2"].label == "host2"  # default label = name
    assert hosts_map["host2"].model == "qwen2.5:14b"
    assert hosts_map["host2"].ollama_host == "http://host2:11434"
    assert hosts_map["mac"].label == "Mac Studio"
    assert hosts_map["mac"].ollama_host == "http://mac:11434"


def test_host_label_extracts_hostname() -> None:
    """`host_label` extracts the hostname from a typical Ollama URL."""
    spec = HostSpec(
        name="x", label="X", description="d", model="m",
        ollama_host="http://host1:11434",
    )
    assert host_label(spec) == "host1"


def test_host_label_returns_none_for_primary_host() -> None:
    """A primary-host spec (ollama_host=None) returns None (template short-circuits)."""
    spec = HostSpec(name="x", label="X", description="d", model="m")
    assert host_label(spec) is None


def test_host_label_returns_none_for_none_spec() -> None:
    """Passing None (primary host — no selection) returns None."""
    assert host_label(None) is None


def test_host_label_falls_back_to_raw_url_when_unparseable() -> None:
    """A value urlparse can't extract a hostname from falls back to the raw
    string — better to show something than swallow the label."""
    spec = HostSpec(
        name="x", label="X", description="d", model="m",
        ollama_host="not-a-url",
    )
    assert host_label(spec) == "not-a-url"


def test_enabled_hosts_includes_slowly_when_toggle_default(_db) -> None:
    """Default state (no row) → toggle is True → the host2 host is included."""
    saved = HOSTS.get("host2")
    HOSTS["host2"] = _slowly_spec()
    try:
        names = {h.name for h in enabled_hosts(_db)}
        assert "host2" in names
    finally:
        if saved is None:
            HOSTS.pop("host2", None)
        else:
            HOSTS["host2"] = saved


def test_enabled_hosts_excludes_slowly_when_toggle_off(_db) -> None:
    """Setting remote_ollama_enabled = False drops every host with a non-None
    ollama_host — i.e. the second host disappears from the picker."""
    queries.set_remote_ollama_enabled(_db, False)

    saved = HOSTS.get("host2")
    HOSTS["host2"] = _slowly_spec()
    try:
        names = {h.name for h in enabled_hosts(_db)}
        assert "host2" not in names
    finally:
        if saved is None:
            HOSTS.pop("host2", None)
        else:
            HOSTS["host2"] = saved


def test_old_loop_symbols_are_gone() -> None:
    """The removed agentic loop must not be importable from app.hosts."""
    for name in (
        "AGENTIC_ITERATION_CAP",
        "RESEARCH_SYSTEM_PROMPT",
        "REVIEW_SYSTEM_PROMPT",
        "GENERATION_SYSTEM_PROMPT",
    ):
        assert not hasattr(hosts, name), name
