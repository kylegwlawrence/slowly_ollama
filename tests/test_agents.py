"""Tests for the Ollama-host registry (formerly the agent registry).

The picker selects an Ollama host: the primary host (``OLLAMA_HOST``) is the
absence of a selection, and an optional "host2" second host
(``SLOWLY_OLLAMA_HOST``) is registered when its env vars are set. The old
persona agents (Research, Content Generator) were removed.
"""

from pathlib import Path

import pytest

from app import agents, queries
from app.agents import (
    AGENTS,
    AgentSpec,
    agent_host_label,
    enabled_agents,
    get_agent,
    list_agents,
)
from app.connection import open_connection
from app.db import initialize_database


@pytest.fixture
def _db(tmp_path: Path):
    """Open a fresh SQLite connection with the schema initialized.

    Used by phase-20b tests that need the ``app_settings`` table to
    drive the ``enabled_agents`` / ``_resolve_active_spec`` toggle.
    """
    db_path = tmp_path / "chats.db"
    initialize_database(db_path)
    with open_connection(db_path) as conn:
        yield conn


def _slowly_spec() -> AgentSpec:
    """A stand-in "host2" host spec for injection into AGENTS in tests."""
    return AgentSpec(
        name="host2", label="host2", description="d",
        model="m", system_prompt="", tools=frozenset(),
        ollama_host="http://host1:11434",
    )


def test_registry_has_no_persona_agents() -> None:
    """The old persona agents are gone; every registered entry is a host.

    The primary host is implicit (not in AGENTS). Any entry present is a
    second host, identified by a non-None ``ollama_host``.
    """
    assert "research" not in AGENTS
    assert "content_generator" not in AGENTS
    for spec in AGENTS.values():
        assert isinstance(spec, AgentSpec)
        assert spec.ollama_host is not None  # a host, not a persona agent


def test_host_specs_are_populated() -> None:
    """Any registered host has the fields a host needs (label, model, host URL).

    ``system_prompt`` / ``tools`` are intentionally empty for hosts, so they
    are NOT asserted non-empty here.
    """
    for spec in AGENTS.values():
        assert spec.name and isinstance(spec.name, str)
        assert spec.label and isinstance(spec.label, str)
        assert spec.model and isinstance(spec.model, str)
        assert spec.ollama_host and isinstance(spec.ollama_host, str)
        assert isinstance(spec.system_prompt, str)
        assert isinstance(spec.tools, frozenset)


def test_degree_architect_removed_from_roster() -> None:
    """Phase 24: the chat-based Degree Architect agent is gone.

    It was replaced by the form-driven /degrees factory
    (app/degree_factory.py). It must NOT be selectable in the chat header.
    """
    assert "degree_architect" not in AGENTS
    assert get_agent("degree_architect") is None


def test_think_defaults_off_and_is_settable() -> None:
    """`think` defaults False (safe on any model) and is opt-in."""
    bare = AgentSpec(
        name="x", label="X", description="d", model="m", system_prompt="p"
    )
    assert bare.think is False

    opted_in = AgentSpec(
        name="y", label="Y", description="d", model="m",
        system_prompt="p", think=True,
    )
    assert opted_in.think is True


def test_get_agent_resolves_names_and_primary() -> None:
    """get_agent maps a known host name to its spec; None/""/unknown → None.

    None (the primary host) and any unknown/removed name both resolve to None
    so the generation layer falls back to plain chat on the primary host.
    """
    assert get_agent(None) is None
    assert get_agent("") is None
    assert get_agent("does_not_exist") is None
    assert get_agent("research") is None  # removed persona agent

    saved = AGENTS.get("host2")
    AGENTS["host2"] = _slowly_spec()
    try:
        assert get_agent("host2") is AGENTS["host2"]
    finally:
        if saved is None:
            AGENTS.pop("host2", None)
        else:
            AGENTS["host2"] = saved


def test_list_agents_returns_registry_in_order() -> None:
    """list_agents preserves insertion (dropdown) order."""
    assert list_agents() == list(AGENTS.values())


def test_agentspec_is_frozen() -> None:
    """Specs are immutable so a stray mutation can't corrupt the registry."""
    import dataclasses

    spec = _slowly_spec()
    try:
        spec.model = "other"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("AgentSpec should be frozen")


def test_agentspec_ollama_host_defaults_none_and_is_settable() -> None:
    """`ollama_host` defaults None (primary/local) and is opt-in per spec."""
    bare = AgentSpec(
        name="x", label="X", description="d", model="m", system_prompt="p"
    )
    assert bare.ollama_host is None

    remote = AgentSpec(
        name="y", label="Y", description="d", model="m",
        system_prompt="p", ollama_host="http://host1:11434",
    )
    assert remote.ollama_host == "http://host1:11434"


def test_build_slowly_host_returns_none_when_env_unset(monkeypatch) -> None:
    """Both env vars missing → no host2 host (matches tool-gating pattern)."""
    from app.agents import _build_slowly_host

    monkeypatch.delenv("SLOWLY_OLLAMA_HOST", raising=False)
    monkeypatch.delenv("SLOWLY_OLLAMA_MODEL", raising=False)
    assert _build_slowly_host() is None


def test_build_slowly_host_returns_none_when_only_host_set(monkeypatch) -> None:
    """Partial config is treated as no config — drop the host."""
    from app.agents import _build_slowly_host

    monkeypatch.setenv("SLOWLY_OLLAMA_HOST", "http://host1:11434")
    monkeypatch.delenv("SLOWLY_OLLAMA_MODEL", raising=False)
    assert _build_slowly_host() is None


def test_build_slowly_host_returns_none_when_only_model_set(monkeypatch) -> None:
    """Partial config is treated as no config — drop the host."""
    from app.agents import _build_slowly_host

    monkeypatch.delenv("SLOWLY_OLLAMA_HOST", raising=False)
    monkeypatch.setenv("SLOWLY_OLLAMA_MODEL", "llama3.1:70b")
    assert _build_slowly_host() is None


def test_build_slowly_host_populated_when_env_set(monkeypatch) -> None:
    """Both env vars set → spec carries the host + pinned model.

    A host is not an agent: ``tools`` and ``system_prompt`` are empty (the
    per-chat chips + project prompt govern, via ``_agent_overrides``).
    """
    from app.agents import _build_slowly_host

    monkeypatch.setenv("SLOWLY_OLLAMA_HOST", "http://host1:11434")
    monkeypatch.setenv("SLOWLY_OLLAMA_MODEL", "llama3.1:70b")
    spec = _build_slowly_host()
    assert spec is not None
    assert spec.name == "host2"
    assert spec.label == "host2"
    assert spec.model == "llama3.1:70b"
    assert spec.ollama_host == "http://host1:11434"
    assert spec.tools == frozenset()
    assert spec.system_prompt == ""


def test_agent_host_label_extracts_hostname() -> None:
    """`agent_host_label` extracts the hostname from a typical Ollama URL."""
    spec = AgentSpec(
        name="x", label="X", description="d", model="m", system_prompt="p",
        ollama_host="http://host1:11434",
    )
    assert agent_host_label(spec) == "host1"


def test_agent_host_label_returns_none_for_primary_host() -> None:
    """A primary-host spec (ollama_host=None) returns None (template short-circuits)."""
    spec = AgentSpec(
        name="x", label="X", description="d", model="m", system_prompt="p",
    )
    assert agent_host_label(spec) is None


def test_agent_host_label_returns_none_for_none_spec() -> None:
    """Passing None (primary host — no selection) returns None."""
    assert agent_host_label(None) is None


def test_agent_host_label_falls_back_to_raw_url_when_unparseable() -> None:
    """A value urlparse can't extract a hostname from falls back to the raw
    string — better to show something than swallow the label."""
    spec = AgentSpec(
        name="x", label="X", description="d", model="m", system_prompt="p",
        ollama_host="not-a-url",
    )
    assert agent_host_label(spec) == "not-a-url"


def test_enabled_agents_includes_slowly_when_toggle_default(_db) -> None:
    """Default state (no row) → toggle is True → the host2 host is included."""
    saved = AGENTS.get("host2")
    AGENTS["host2"] = _slowly_spec()
    try:
        names = {a.name for a in enabled_agents(_db)}
        assert "host2" in names
    finally:
        if saved is None:
            AGENTS.pop("host2", None)
        else:
            AGENTS["host2"] = saved


def test_enabled_agents_excludes_slowly_when_toggle_off(_db) -> None:
    """Setting remote_ollama_enabled = False drops every host with a non-None
    ollama_host — i.e. the second host disappears from the picker."""
    queries.set_remote_ollama_enabled(_db, False)

    saved = AGENTS.get("host2")
    AGENTS["host2"] = _slowly_spec()
    try:
        names = {a.name for a in enabled_agents(_db)}
        assert "host2" not in names
    finally:
        if saved is None:
            AGENTS.pop("host2", None)
        else:
            AGENTS["host2"] = saved


def test_old_loop_symbols_are_gone() -> None:
    """The removed agentic loop must not be importable from app.agents."""
    for name in (
        "AGENTIC_ITERATION_CAP",
        "RESEARCH_SYSTEM_PROMPT",
        "REVIEW_SYSTEM_PROMPT",
        "GENERATION_SYSTEM_PROMPT",
    ):
        assert not hasattr(agents, name), name
