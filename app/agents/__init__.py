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

import sqlite3
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.agents.prompts import (
    CONTENT_GENERATOR_PROMPT,
    REMOTE_AGENT_PROMPT,
    RESEARCH_AGENT_PROMPT,
)
from app.config import remote_ollama_host, remote_ollama_model
from app.queries.settings import get_remote_ollama_enabled


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
        ollama_host: When set, this agent's Ollama calls (chat probe, stream,
            compaction) target this base URL instead of the local ``OLLAMA_HOST``.
            ``None`` runs the agent on the local Ollama like every other call.
            Tools still execute on this server — only inference is offloaded.
    """

    name: str
    label: str
    description: str
    model: str
    system_prompt: str
    tools: frozenset[str] = field(default_factory=frozenset)
    think: bool = False
    ollama_host: str | None = None


# Insertion order is the dropdown order. "Normal" is rendered by the UI as a
# leading option and is NOT in this dict — it maps to `active_agent = None`.
AGENTS: dict[str, AgentSpec] = {
    "research": AgentSpec(
        name="research",
        label="Research",
        description="Gathers information with tools and reports findings.",
        model="granite4.1:8b",
        system_prompt=RESEARCH_AGENT_PROMPT,
        tools=frozenset({"current_time", "query_rag", "fetch_github_file"}),
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
    # Note: the chat-based "degree_architect" agent (Phase 23 Phase A) was
    # removed in Phase 24 — it overloaded context by re-sending the whole
    # conversation each tool turn. It's replaced by the form-driven /degrees
    # factory (app/degree_factory.py), which generates the outline in small,
    # independent calls. Its prompt text lives on in app/agents/prompts.py
    # (DEGREE_ARCHITECT_PROMPT) — the factory mines its rule snippets.
}


def _build_remote_agent() -> AgentSpec | None:
    """Return the "Remote" AgentSpec when its env vars are set, else None.

    Same gating pattern as the file tools and query_rag — when EITHER env
    var is missing we drop the agent from the registry entirely rather
    than register a degenerate version that would fail on its first turn.
    Tools still execute on this server; only inference is offloaded.

    Extracted as a function (not inlined) so tests can drive it with
    monkeypatched env without reimporting the module.

    Returns:
        A populated AgentSpec when both REMOTE_OLLAMA_HOST and
        REMOTE_OLLAMA_MODEL are set; None otherwise.
    """
    host = remote_ollama_host()
    model = remote_ollama_model()
    if not host or not model:
        return None
    return AgentSpec(
        name="remote",
        label="Remote",
        description="General-purpose agent running on a second Ollama instance.",
        model=model,
        system_prompt=REMOTE_AGENT_PROMPT,
        # Allow every tool name we know about. Tools gated off at runtime
        # (file tools without FILE_TOOL_ROOT, query_rag without configured
        # servers) are simply absent from TOOLS and the allowlist filter
        # skips them — same fallthrough as the other agents.
        tools=frozenset({
            "current_time",
            "read_file",
            "write_file",
            "list_directory",
            "search_files",
            "query_rag",
            "fetch_github_file",
        }),
        # think=False is safe on any model. If the remote model is
        # thinking-capable and reasoning is wanted, flip this and ship.
        think=False,
        ollama_host=host,
    )


_remote_agent = _build_remote_agent()
if _remote_agent is not None:
    AGENTS[_remote_agent.name] = _remote_agent


def list_agents() -> list[AgentSpec]:
    """Return all registered agents in dropdown order."""
    return list(AGENTS.values())


def enabled_agents(conn: sqlite3.Connection) -> list[AgentSpec]:
    """Return agents the user should currently see in the picker.

    Drops any agent whose ``ollama_host`` is set when the app-wide Remote
    Ollama toggle is off (``app_settings.remote_ollama_enabled = "0"``).
    Local agents (``ollama_host is None``) always pass through. With the
    toggle on this is the same set as :func:`list_agents`.

    Routes use this for rendering the dropdown so a disabled remote agent
    disappears from the UI without the registry having to be rebuilt.

    Args:
        conn: Open SQLite connection — the toggle lives in ``app_settings``.
    """
    if get_remote_ollama_enabled(conn):
        return list(AGENTS.values())
    return [a for a in AGENTS.values() if a.ollama_host is None]


def agent_host_label(spec: AgentSpec | None) -> str | None:
    """Human-readable hostname for an agent's ``ollama_host``, or None.

    Used by the chat header chip so the user can see at a glance which
    machine an agent runs on (e.g. ``"host1"`` for
    ``http://host1:11434``). Local agents (``ollama_host is None``)
    return ``None`` so the template can short-circuit without rendering
    the suffix.

    Falls back to the raw ``ollama_host`` value if ``urlparse`` can't
    extract a hostname — better to show *something* than swallow the
    label entirely.

    Args:
        spec: An ``AgentSpec`` or ``None``. ``None`` returns ``None``
            so the template can pass the active spec through without
            a guard.

    Returns:
        The hostname portion of the agent's ``ollama_host``, the raw
        value when parsing fails, or ``None`` when the agent runs on
        local Ollama (or ``spec`` is ``None``).
    """
    if spec is None or not spec.ollama_host:
        return None
    parsed = urlparse(spec.ollama_host)
    return parsed.hostname or spec.ollama_host


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


__all__ = [
    "AgentSpec",
    "AGENTS",
    "agent_host_label",
    "enabled_agents",
    "get_agent",
    "list_agents",
]
