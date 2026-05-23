"""Phase 16: registry of user-invoked agents.

An agent is a named bundle of (system prompt, assigned Ollama model, tool
allowlist). The user picks one from the composer dropdown; that agent's turn
runs the ordinary single-agent producer (`app.generation._run_generation`)
parameterized by those three things. "Normal" (plain chat) is the *absence* of
an agent — see `get_agent`.

Code-defined on purpose (mirrors `app/tools/__init__.py` and the hardcoded
prompts): adding an agent is a few lines here plus a restart, version-controlled
and testable, with no runtime CRUD surface to maintain. New agents are expected
to be added over time by editing `AGENTS`.
"""

from dataclasses import dataclass, field

from app.agents.prompts import CONTENT_GENERATOR_PROMPT, RESEARCH_AGENT_PROMPT


@dataclass(frozen=True)
class AgentSpec:
    """A user-invokable agent definition.

    Attributes:
        name: Stable identifier persisted on the conversation
            (`conversations.active_agent`) and used as the dropdown's option
            value. Lowercase snake_case.
        label: Human-readable name shown in the UI.
        description: One-line summary for the dropdown / tooltip.
        model: Ollama model id this agent always runs on, regardless of the
            chat's pinned model. Must be installed; tool-using agents need a
            tool-capable model.
        system_prompt: The ``system``-role message prepended to every turn.
        tools: Allowlist of tool names (keys in `app.tools.TOOLS`) this agent
            may call. An empty set means the agent runs with no tools.
        think: Whether to enable the model's reasoning/"thinking" phase
            (Ollama's ``think`` flag). Defaults to ``False``, which is safe on
            ANY model and stops chatty models (e.g. qwen) from over-reasoning
            before answering. Set ``True`` ONLY for an agent assigned a
            thinking-capable model — Ollama returns a 400 for ``think: true``
            on a model without the capability.
    """

    name: str
    label: str
    description: str
    model: str
    system_prompt: str
    tools: frozenset[str] = field(default_factory=frozenset)
    think: bool = False


# Insertion order is the dropdown order. "Normal" is rendered by the UI as a
# leading option and is NOT in this dict — it maps to `active_agent = None`.
AGENTS: dict[str, AgentSpec] = {
    "research": AgentSpec(
        name="research",
        label="Research",
        description="Gathers information with tools and reports findings.",
        model="granite4.1:8b",
        system_prompt=RESEARCH_AGENT_PROMPT,
        tools=frozenset({"current_time", "query_rag"}),
        # granite4.1:8b is tool-capable and fast on 16GB. It is NOT a
        # thinking model, so think MUST stay False — Ollama 400s on
        # think=true for a model without the capability.
        think=False,
    ),
    "content_generator": AgentSpec(
        name="content_generator",
        label="Content Generator",
        description="Writes a polished piece from the conversation so far.",
        model="granite4.1:8b",
        system_prompt=CONTENT_GENERATOR_PROMPT,
        # read_file / write_file / list_directory let it browse, draft
        # into, and revise files in the workspace. Gated on FILE_TOOL_ROOT:
        # when that's unset all three are absent from TOOLS, and
        # _run_generation's allowlist filter simply drops them — the agent
        # degrades to tool-less synthesis.
        tools=frozenset({"read_file", "write_file", "list_directory", "search_files"}),
        # Shares Research's model (granite4.1:8b) so the Research -> Content
        # hand-off needs no model swap/reload on a 16GB machine. Not a
        # thinking model, so think stays False.
        think=False,
    ),
}


def list_agents() -> list[AgentSpec]:
    """Return all registered agents in dropdown order."""
    return list(AGENTS.values())


def get_agent(name: str | None) -> AgentSpec | None:
    """Resolve an agent name to its spec.

    Args:
        name: The stored/submitted agent name, or None/"" for Normal.

    Returns:
        The matching `AgentSpec`, or None for Normal (empty/missing name) or an
        unknown name (defensive — e.g. a name persisted before an agent was
        removed from the registry). A None result means "run plain chat".
    """
    if not name:
        return None
    return AGENTS.get(name)


__all__ = ["AgentSpec", "AGENTS", "list_agents", "get_agent"]
