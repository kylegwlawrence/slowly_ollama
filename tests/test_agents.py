"""Phase 16: tests for the user-invoked agent registry."""

from app import agents
from app.agents import AGENTS, AgentSpec, get_agent, list_agents


def test_registry_contains_expected_agents() -> None:
    """The shipped roster is research + content_generator (Normal is implicit)."""
    assert set(AGENTS) == {"research", "content_generator"}
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
    assert research.tools == frozenset({"current_time", "query_rag"})

    content = AGENTS["content_generator"]
    assert content.tools == frozenset({"read_file", "write_file", "list_directory", "search_files"})


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

    # query_rag is registered only when a RAG server is configured, and the
    # file tools only when FILE_TOOL_ROOT is set — both are absent from TOOLS
    # in a bare test env, so allow them explicitly.
    known = set(TOOLS) | {"query_rag", "read_file", "write_file"}
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


def test_old_loop_symbols_are_gone() -> None:
    """The removed agentic loop must not be importable from app.agents."""
    for name in (
        "AGENTIC_ITERATION_CAP",
        "RESEARCH_SYSTEM_PROMPT",
        "REVIEW_SYSTEM_PROMPT",
        "GENERATION_SYSTEM_PROMPT",
    ):
        assert not hasattr(agents, name), name
