"""Registry of selectable Ollama *hosts* for the per-chat picker.

Originally a registry of user-invoked agents (Phase 16); repurposed into an
Ollama-host selector. The primary host (``OLLAMA_HOST``) is the *absence* of a
selection — the picker's leading "host1" option, ``active_agent`` NULL (see
``get_agent``). An optional second host ("host2", ``SLOWLY_OLLAMA_HOST``) is
registered when its env vars are set; selecting it routes a chat's inference
to that machine on its pinned model, but otherwise behaves like plain chat —
the per-chat tool/RAG chips and the project prompt still apply (see
``app.routes._helpers._agent_overrides``).

The storage column (``conversations.active_agent``), the
``/chats/{id}/agent`` route, and the ``AgentSpec`` dataclass keep their
original names to avoid a migration; only the registry contents and the
user-facing labels reflect the host framing.
"""

import sqlite3
from dataclasses import dataclass, field
from urllib.parse import urlparse

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


# The primary host ("host1") is the *absence* of a selection (active_agent
# NULL); the UI renders it as the leading picker option and it is NOT in this
# dict. The only registered entry is the optional "host2" second host, added
# below when both SLOWLY_OLLAMA_HOST and SLOWLY_OLLAMA_MODEL are set.
AGENTS: dict[str, AgentSpec] = {}


def _build_slowly_host() -> AgentSpec | None:
    """Return the "host2" host spec when its env vars are set, else None.

    Same both-or-nothing gating as the file tools and ``query_rag`` — when
    EITHER ``SLOWLY_OLLAMA_HOST`` or ``SLOWLY_OLLAMA_MODEL`` is missing we drop
    the host from the registry rather than register a half-configured one that
    would fail on its first turn.

    The spec carries only what a host needs — a pinned model and the host URL.
    ``system_prompt`` and ``tools`` are intentionally left empty:
    ``_agent_overrides`` routes a selected host through the plain-chat path
    (per-chat chips + project prompt), so neither field is consulted. Extracted
    as a function (not inlined) so tests can drive it with monkeypatched env
    without reimporting the module.

    Returns:
        A populated AgentSpec when both ``SLOWLY_OLLAMA_HOST`` and
        ``SLOWLY_OLLAMA_MODEL`` are set; None otherwise.
    """
    host = remote_ollama_host()
    model = remote_ollama_model()
    if not host or not model:
        return None
    return AgentSpec(
        name="host2",
        label="host2",
        description="Run this chat on the 'host2' Ollama host.",
        model=model,
        # Empty — a host is not an agent. _agent_overrides ignores these and
        # uses the per-chat chips + project prompt, exactly like the primary.
        system_prompt="",
        tools=frozenset(),
        think=False,
        ollama_host=host,
    )


_slowly_host = _build_slowly_host()
if _slowly_host is not None:
    AGENTS[_slowly_host.name] = _slowly_host


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
