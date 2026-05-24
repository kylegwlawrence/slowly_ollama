"""Shared helpers used by multiple route sub-modules.

Lives alongside the sub-routers so they can each import the per-render
context, the chip-state lookup, the agent-overrides resolver, etc. from
one place rather than reaching across to a sibling sub-module.
"""

import sqlite3

from app import ollama, queries
from app import rag_servers as _rag_servers
from app.agents import get_agent, list_agents
from app.tools import RAG_TOOL_NAME, TOOLS


# All currently-registered tool names, in registration order.
# Computed once at import time. Used for seeding and generation filtering.
_ALL_TOOL_NAMES: list[str] = list(TOOLS.keys())


def _default_tool_states() -> list[queries.ChatToolState]:
    """Return ChatToolState list with all non-RAG tools enabled.

    query_rag is excluded — RAG servers get their own per-server chips.
    """
    return [
        queries.ChatToolState(tool_name=name, enabled=True)
        for name in _ALL_TOOL_NAMES
        # `name in TOOLS` excludes tools the lifespan gating popped (e.g.
        # the file tools when FILE_TOOL_ROOT is unset) so a misconfigured
        # install doesn't seed chips for a tool the model can't see.
        if name != RAG_TOOL_NAME and name in TOOLS
    ]


def _default_rag_server_states(
    db: sqlite3.Connection,
) -> list[queries.ChatRagState]:
    """Return ChatRagState list with all configured RAG servers enabled.

    Used by the empty-state composer so per-server chips default to on
    before a chat is created.
    """
    servers = _rag_servers.list_servers(db)
    return [
        queries.ChatRagState(server_name=s.name, enabled=True) for s in servers
    ]


def _chip_states(
    db: sqlite3.Connection,
    conversation_id: int,
    *,
    servers: list | None = None,
) -> tuple[list[queries.ChatToolState], list[queries.ChatRagState]]:
    """Return (tool_states, rag_server_states) for the chip bar.

    tool_states excludes query_rag; RAG servers get their own chips.
    Both lists respect the per-chat settings stored in DB.

    Pass ``servers`` when you already hold the list from a prior
    ``_rag_servers.list_servers`` call to avoid a redundant fetch.
    """
    tool_states = [
        s
        for s in queries.get_chat_tool_states(
            db,
            conversation_id,
            # Live-filter so gating-popped tools (e.g. file tools when
            # FILE_TOOL_ROOT is unset) don't surface as chips.
            [n for n in _ALL_TOOL_NAMES if n in TOOLS],
        )
        if s.tool_name != RAG_TOOL_NAME
    ]
    if servers is None:
        servers = _rag_servers.list_servers(db)
    rag_server_states = queries.get_chat_rag_states(
        db, conversation_id, [s.name for s in servers]
    )
    return tool_states, rag_server_states


def _placeholder_name(content: str) -> str:
    """Derive a sidebar-friendly placeholder name from a user message.

    Used by ``create_project_chat_endpoint`` to give every new chat an
    immediately-identifiable sidebar entry from the moment it's
    created, instead of a generic "New chat" everyone has. The
    phase 11d auto-titler may replace it later with a cleaner
    model-generated summary.

    Args:
        content: The user's first message. Multi-line content is
            collapsed to the first non-empty line; whitespace
            is trimmed. The 40-char cap fits a 280px-wide sidebar
            without truncation ellipses kicking in.

    Returns:
        Up to 40 chars of the first non-empty line. Falls back to
        ``"New chat"`` if ``content`` is empty or whitespace-only
        (POST /chats requires content via Form() so this is
        mostly a defensive fallback).
    """
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:40]
    return "New chat"


def _resolve_num_ctx(
    db: sqlite3.Connection, conversation_id: int
) -> int:
    """Effective Ollama ``num_ctx`` for a turn: project override → global.

    Looks up the project that owns ``conversation_id`` and returns
    ``project.num_ctx`` when set, otherwise the global default
    (``queries.get_default_num_ctx``). Falls back to the global default
    when the project can't be resolved (defensive — every chat should
    have a project post-phase-17).
    """
    try:
        project = queries.get_project_for_conversation(db, conversation_id)
    except LookupError:
        return queries.get_default_num_ctx(db)
    return queries.resolve_num_ctx_for_project(db, project.num_ctx)


def _agent_overrides(conversation: queries.Conversation) -> dict:
    """Resolve a conversation's active agent into ``start_generation`` kwargs.

    Returns the effective ``model`` plus ``system_prompt_override`` /
    ``tool_allowlist`` / ``think``. For Normal chat (no agent, or an
    unknown/removed agent name) this is the chat's pinned model with no
    overrides and ``think=None`` (omit the flag) — i.e. today's plain-chat
    behavior.
    """
    spec = get_agent(conversation.active_agent)
    if spec is None:
        return {
            "model": conversation.model,
            "system_prompt_override": None,
            "tool_allowlist": None,
            "think": None,
        }
    return {
        "model": spec.model,
        "system_prompt_override": spec.system_prompt,
        "tool_allowlist": spec.tools,
        "think": spec.think,
    }


def _project_context(
    db: sqlite3.Connection,
    *,
    composer: bool = False,
) -> dict:
    """Build the shared context for project page renders.

    Pulls together global defaults the composer / chat panel need —
    default temperature, default tool cap, agent registry, current
    RAG-server states. Centralized here so every project endpoint
    renders with the same shape.

    Args:
        db: Open SQLite connection.
        composer: When True, also includes the per-composer default
            tool / RAG chip states (only meaningful on Chats-tab empty
            state where the composer renders).
    """
    ctx = {
        "default_temperature": queries.get_default_temperature(db),
        "default_tool_iteration_cap": queries.get_default_tool_iteration_cap(db),
        "global_default_model": queries.get_default_model(db),
        "agents": list_agents(),
    }
    if composer:
        ctx["default_tool_states"] = _default_tool_states()
        ctx["default_rag_server_states"] = _default_rag_server_states(db)
    return ctx
