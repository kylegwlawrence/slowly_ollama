"""Phase 16: tests for the user-invoked agent registry."""

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


def test_registry_contains_expected_agents() -> None:
    """The shipped roster is at least research + content_generator (Normal is
    implicit); the optional "remote" agent appears only when its env vars are
    set, so use a superset check rather than strict equality."""
    assert {"research", "content_generator"} <= set(AGENTS)
    for spec in AGENTS.values():
        assert isinstance(spec, AgentSpec)


def test_agentspec_fields_are_populated() -> None:
    """Every agent has a label, description, model, prompt, and tools set."""
    for spec in AGENTS.values():
        assert spec.name and isinstance(spec.name, str)
        assert spec.label and isinstance(spec.label, str)
        assert spec.description and isinstance(spec.description, str)
        assert spec.model and isinstance(spec.model, str)
        assert isinstance(spec.system_prompt, str)
        assert len(spec.system_prompt.strip()) > 100  # not a placeholder
        assert isinstance(spec.tools, frozenset)


def test_shipped_agent_allowlists() -> None:
    """Research retrieves; the content generator reads/writes/browses workspace files."""
    research = AGENTS["research"]
    assert research.tools == frozenset(
        {"current_time", "query_rag", "fetch_github_file"}
    )

    content = AGENTS["content_generator"]
    assert content.tools == frozenset({"read_file", "write_file", "list_directory", "search_files"})


def test_degree_architect_registration() -> None:
    """Phase 23: Architect is registered with the locked-in model + tools.

    The model choice (qwen2.5-coder:7b vs. the granite4.1:8b family used by
    the other agents) is intentional — Qwen Coder is unusually strong on
    structured-JSON output, which the Architect's Phase-3 assembly step
    depends on. Pinned here so a future edit that swaps it on a hunch fails
    loudly until the test is updated to match the new rationale.
    """
    architect = AGENTS["degree_architect"]

    assert architect.name == "degree_architect"
    assert architect.label == "Degree Architect"
    assert architect.model == "qwen2.5-coder:7b"
    assert architect.tools == frozenset({
        "read_file",
        "write_file",
        "list_directory",
        "query_rag",
        "fetch_github_file",
    })
    # qwen2.5-coder is not a thinking model — think must stay False or
    # Ollama 400s on the request.
    assert architect.think is False
    # Architect runs on local Ollama (no host pin) — it's the highest-stakes
    # human-in-loop call, kept near the user.
    assert architect.ollama_host is None
    # Prompt is preserved verbatim in code (not a placeholder).
    assert "Degree Architect" in architect.system_prompt
    assert "Phase 1: Interview" in architect.system_prompt
    assert "Phase 2: Outline build" in architect.system_prompt
    assert "Phase 3: Assemble" in architect.system_prompt


def test_think_defaults_off_and_is_settable() -> None:
    """`think` defaults False (safe on any model) and is opt-in.

    Both shipped agents currently run on non-thinking models, so both are
    False — Research on granite4.1:8b (which 400s on think=true) and the
    Content Generator for directness. The flag is still settable to True for
    a future agent assigned a thinking-capable model."""
    # Default is the safe value.
    bare = AgentSpec(
        name="x", label="X", description="d", model="m", system_prompt="p"
    )
    assert bare.think is False

    # ...but opt-in still works for a thinking-capable model.
    opted_in = AgentSpec(
        name="y", label="Y", description="d", model="m",
        system_prompt="p", think=True,
    )
    assert opted_in.think is True

    assert AGENTS["research"].think is False
    assert AGENTS["content_generator"].think is False


def test_shipped_agent_tools_are_real_registered_tools() -> None:
    """An agent's allowlist must reference tools that actually exist, so a
    typo can't silently produce an agent that offers a non-existent tool."""
    from app.tools import TOOLS

    # query_rag is registered only when a RAG server is configured, the file
    # tools only when FILE_TOOL_ROOT is set, and fetch_github_file only when
    # its module is imported (the conftest skips it). All are absent from
    # TOOLS in a bare test env, so allow them explicitly.
    known = set(TOOLS) | {
        "query_rag",
        "read_file",
        "write_file",
        "fetch_github_file",
    }
    for spec in AGENTS.values():
        assert spec.tools <= known, spec.tools - known


def test_get_agent_resolves_names_and_normal() -> None:
    """get_agent maps a name to its spec; None/""/unknown → None (Normal)."""
    assert get_agent("research") is AGENTS["research"]
    assert get_agent("content_generator") is AGENTS["content_generator"]
    assert get_agent(None) is None
    assert get_agent("") is None
    assert get_agent("does_not_exist") is None


def test_list_agents_returns_registry_in_order() -> None:
    """list_agents preserves insertion (dropdown) order."""
    assert list_agents() == list(AGENTS.values())


def test_agentspec_is_frozen() -> None:
    """Specs are immutable so a stray mutation can't corrupt the registry."""
    import dataclasses

    spec = AGENTS["research"]
    try:
        spec.model = "other"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("AgentSpec should be frozen")


def test_agentspec_ollama_host_defaults_none_and_is_settable() -> None:
    """`ollama_host` defaults None (local Ollama) and is opt-in per agent."""
    bare = AgentSpec(
        name="x", label="X", description="d", model="m", system_prompt="p"
    )
    assert bare.ollama_host is None

    remote = AgentSpec(
        name="y", label="Y", description="d", model="m",
        system_prompt="p", ollama_host="http://host1:11434",
    )
    assert remote.ollama_host == "http://host1:11434"


def test_build_remote_agent_returns_none_when_env_unset(monkeypatch) -> None:
    """Both env vars missing → no remote agent (matches tool-gating pattern)."""
    from app.agents import _build_remote_agent

    monkeypatch.delenv("REMOTE_OLLAMA_HOST", raising=False)
    monkeypatch.delenv("REMOTE_OLLAMA_MODEL", raising=False)
    assert _build_remote_agent() is None


def test_build_remote_agent_returns_none_when_only_host_set(monkeypatch) -> None:
    """Partial config is treated as no config — drop the agent."""
    from app.agents import _build_remote_agent

    monkeypatch.setenv("REMOTE_OLLAMA_HOST", "http://host1:11434")
    monkeypatch.delenv("REMOTE_OLLAMA_MODEL", raising=False)
    assert _build_remote_agent() is None


def test_build_remote_agent_returns_none_when_only_model_set(monkeypatch) -> None:
    """Partial config is treated as no config — drop the agent."""
    from app.agents import _build_remote_agent

    monkeypatch.delenv("REMOTE_OLLAMA_HOST", raising=False)
    monkeypatch.setenv("REMOTE_OLLAMA_MODEL", "llama3.1:70b")
    assert _build_remote_agent() is None


def test_build_remote_agent_populated_when_env_set(monkeypatch) -> None:
    """Both env vars set → spec carries the host + model + a non-empty allowlist."""
    from app.agents import _build_remote_agent

    monkeypatch.setenv("REMOTE_OLLAMA_HOST", "http://host1:11434")
    monkeypatch.setenv("REMOTE_OLLAMA_MODEL", "llama3.1:70b")
    spec = _build_remote_agent()
    assert spec is not None
    assert spec.name == "remote"
    assert spec.model == "llama3.1:70b"
    assert spec.ollama_host == "http://host1:11434"
    # Allowlist names every shipped tool — tools missing from TOOLS at runtime
    # (file tools without FILE_TOOL_ROOT, query_rag without servers) are
    # silently dropped by `_agent_tool_specs`.
    assert "current_time" in spec.tools
    assert "query_rag" in spec.tools
    assert "fetch_github_file" in spec.tools


def test_agent_host_label_extracts_hostname() -> None:
    """`agent_host_label` extracts the hostname from a typical Ollama URL."""
    spec = AgentSpec(
        name="x", label="X", description="d", model="m", system_prompt="p",
        ollama_host="http://host1:11434",
    )
    assert agent_host_label(spec) == "host1"


def test_agent_host_label_returns_none_for_local_agent() -> None:
    """A local agent (ollama_host=None) returns None so the template short-circuits."""
    spec = AgentSpec(
        name="x", label="X", description="d", model="m", system_prompt="p",
    )
    assert agent_host_label(spec) is None


def test_agent_host_label_returns_none_for_none_spec() -> None:
    """Passing None (Normal chat — no active agent) returns None."""
    assert agent_host_label(None) is None


def test_agent_host_label_falls_back_to_raw_url_when_unparseable() -> None:
    """A value urlparse can't extract a hostname from falls back to the raw
    string — better to show something than swallow the label."""
    spec = AgentSpec(
        name="x", label="X", description="d", model="m", system_prompt="p",
        ollama_host="not-a-url",
    )
    # urlparse parses "not-a-url" as a path with no hostname; we fall back
    # to the raw value.
    assert agent_host_label(spec) == "not-a-url"


def test_enabled_agents_includes_remote_when_toggle_default(_db) -> None:
    """Default state (no row) → toggle is True → remote agent (if present)
    is included alongside local agents."""
    # Inject a remote spec directly into AGENTS so the test doesn't depend on
    # REMOTE_OLLAMA_HOST env state. Snapshot + restore so other tests aren't
    # affected.
    saved = AGENTS.get("remote")
    AGENTS["remote"] = AgentSpec(
        name="remote", label="Remote", description="d",
        model="m", system_prompt="p", ollama_host="http://host1:11434",
    )
    try:
        names = {a.name for a in enabled_agents(_db)}
        assert "remote" in names
    finally:
        if saved is None:
            AGENTS.pop("remote", None)
        else:
            AGENTS["remote"] = saved


def test_enabled_agents_excludes_remote_when_toggle_off(_db) -> None:
    """Setting remote_ollama_enabled = False drops every agent with a non-None
    ollama_host. Local agents (Research, Content Generator) are untouched."""
    queries.set_remote_ollama_enabled(_db, False)

    saved = AGENTS.get("remote")
    AGENTS["remote"] = AgentSpec(
        name="remote", label="Remote", description="d",
        model="m", system_prompt="p", ollama_host="http://host1:11434",
    )
    try:
        names = {a.name for a in enabled_agents(_db)}
        assert "remote" not in names
        # Local agents stay regardless of the toggle.
        assert "research" in names
        assert "content_generator" in names
    finally:
        if saved is None:
            AGENTS.pop("remote", None)
        else:
            AGENTS["remote"] = saved


def test_old_loop_symbols_are_gone() -> None:
    """The removed agentic loop must not be importable from app.agents."""
    for name in (
        "AGENTIC_ITERATION_CAP",
        "RESEARCH_SYSTEM_PROMPT",
        "REVIEW_SYSTEM_PROMPT",
        "GENERATION_SYSTEM_PROMPT",
    ):
        assert not hasattr(agents, name), name
