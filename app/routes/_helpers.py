"""Shared helpers used by multiple route sub-modules.

Lives alongside the sub-routers so they can each import the per-render
context, the chip-state lookup, the agent-overrides resolver, etc. from
one place rather than reaching across to a sibling sub-module.
"""

import asyncio
import sqlite3

from app import ollama, queries, rag_health
from app import rag_servers as _rag_servers
from app.agents import enabled_agents, get_agent
from app.tools import RAG_TOOL_NAME, TOOLS


def _default_tool_states() -> list[queries.ChatToolState]:
    """Return ChatToolState list with all non-RAG tools enabled.

    query_rag is excluded — RAG servers get their own per-server chips.
    Reads ``TOOLS`` live (not a startup snapshot) so a tool whose @tool
    decorator runs after this module imports — or which the lifespan
    gating later popped — is reflected on the next call without needing
    a specific import order in ``main.py``.
    """
    return [
        queries.ChatToolState(tool_name=name, enabled=True)
        for name in TOOLS
        if name != RAG_TOOL_NAME
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
            # Iterate the live registry so gating-popped tools (e.g. file
            # tools when FILE_TOOL_ROOT is unset) don't surface and tools
            # registered after this module imported still do.
            list(TOOLS),
        )
        if s.tool_name != RAG_TOOL_NAME
    ]
    if servers is None:
        servers = _rag_servers.list_servers(db)
    rag_server_states = queries.get_chat_rag_states(
        db, conversation_id, [s.name for s in servers]
    )
    return tool_states, rag_server_states


def _spawn_health_refresh(db: sqlite3.Connection) -> None:
    """Fire-and-forget RAG-server health refresh (phase 19).

    Called from send / regenerate / create-chat endpoints right after
    ``start_generation`` so the cache is freshly populated by the time
    the next sidebar render asks for it. Doesn't block the response —
    the user shouldn't pay probe latency on every send.

    A server that went down between chat-open and send-time will show
    red on the next sidebar render (chat switch, page reload, chip
    toggle response). Within the current request the user already
    pressed Send, so a "warn before send" UI isn't possible here
    anyway; ``query_rag`` will surface the actual failure mid-stream.
    """
    servers = _rag_servers.list_servers(db)
    if servers:
        asyncio.create_task(rag_health.get_health_map(servers, force=True))


async def _sidebar_rag_context(
    db: sqlite3.Connection,
    conversation: queries.Conversation | None,
    *,
    supports_tools: bool,
    servers: list | None = None,
) -> dict:
    """Build the context vars the sidebar's RAG section needs (phase 19).

    Returns keys consumable by ``_sidebar.html``'s guard:
      ``active_conversation``
      ``active_chat_supports_tools``
      ``active_chat_agent_active``
      ``rag_server_states``
      ``rag_health``

    When ``conversation`` is None (no active chat), returns the minimal
    shape with empty lists — the guard in ``_sidebar.html`` shorts the
    section to hidden. When no servers are configured, also returns empty
    ``rag_health`` (no need to probe).

    The RAG chips apply on either Ollama host (the picker selects a host,
    not an agent with its own allowlist), so ``active_chat_agent_active`` is
    always False — the section is governed solely by tool support + whether
    any servers are configured.

    Probes server health in parallel via the cache; render-time cost is
    one ``asyncio.gather`` per chat-panel render (cache-hit fast path:
    sub-millisecond).
    """
    if conversation is None or not supports_tools:
        return {
            "active_conversation": conversation,
            "active_chat_supports_tools": supports_tools,
            "active_chat_agent_active": False,
            "rag_server_states": [],
            "rag_health": {},
        }

    if servers is None:
        servers = _rag_servers.list_servers(db)
    rag_server_states = queries.get_chat_rag_states(
        db, conversation.id, [s.name for s in servers]
    )
    if not servers:
        return {
            "active_conversation": conversation,
            "active_chat_supports_tools": supports_tools,
            "active_chat_agent_active": False,
            "rag_server_states": rag_server_states,
            "rag_health": {},
        }

    health_map = await rag_health.get_health_map(servers)
    return {
        "active_conversation": conversation,
        "active_chat_supports_tools": supports_tools,
        "active_chat_agent_active": False,
        "rag_server_states": rag_server_states,
        "rag_health": health_map,
    }


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


def _resolve_active_spec(
    conversation: queries.Conversation, db: sqlite3.Connection
):
    """Return the effective AgentSpec for a conversation, honoring the toggle.

    Wraps :func:`app.agents.get_agent` so callers that render the chat
    header (model chip, agent indicator, unload button) all see the same
    "what's actually going to run" view: a chat with
    ``active_agent="host2"`` resolves to ``None`` when the phase-20b
    Remote Ollama toggle is off, so the indicator shows the primary host's
    pinned model instead of the now-disabled second host.

    Args:
        conversation: The chat whose ``active_agent`` to resolve.
        db: Open SQLite connection — the toggle lives in ``app_settings``.

    Returns:
        The resolved ``AgentSpec``, or ``None`` for the primary host (no
        selection, unknown name, OR a second-host spec while the toggle is
        off).
    """
    spec = get_agent(conversation.active_agent)
    if spec is not None and spec.ollama_host is not None:
        if not queries.get_remote_ollama_enabled(db):
            return None
    return spec


def _agent_overrides(
    conversation: queries.Conversation, db: sqlite3.Connection
) -> dict:
    """Resolve a conversation's selected Ollama host into ``start_generation`` kwargs.

    The per-chat picker selects a host, stored in
    ``conversation.active_agent``: NULL/empty (or an unknown/removed name)
    means the primary host ("host1"); a known name (e.g. ``"host2"``) means
    that registered second host.

    The primary host returns the chat's pinned model with no overrides,
    ``think=None`` (omit the flag), and ``ollama_host=None`` (local). A
    selected second host returns its ``ollama_host`` and the chat's per-host
    model (``conversation.slowly_model``, falling back to the host spec's
    default ``SLOWLY_OLLAMA_MODEL``), but is otherwise IDENTICAL:
    ``tool_allowlist=None`` and
    ``system_prompt_override=None`` route it through the plain-chat generation
    path, so the per-chat tool/RAG chips and the project system prompt apply on
    either host — only the machine and the model differ. The host spec's own
    ``system_prompt`` / ``tools`` fields are deliberately ignored (a host is
    not an agent).

    Phase 20b gating still applies: when a selected host's spec has a non-None
    ``ollama_host`` AND the app-wide ``remote_ollama_enabled`` toggle is off,
    ``_resolve_active_spec`` returns None and we fall back to the primary host
    — chats with ``active_agent="host2"`` then run plain on the chat's pinned
    local model, no data loss, regardless of whether the second host is up.
    """
    spec = _resolve_active_spec(conversation, db)
    if spec is None:
        return {
            "model": conversation.model,
            "system_prompt_override": None,
            "tool_allowlist": None,
            "think": None,
            "ollama_host": None,
        }
    # A selected second host ("host2") uses the chat's per-host model when
    # set, falling back to the host spec's default (SLOWLY_OLLAMA_MODEL).
    return {
        "model": conversation.slowly_model or spec.model,
        "system_prompt_override": None,
        "tool_allowlist": None,
        "think": None,
        "ollama_host": spec.ollama_host,
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
        # enabled_agents respects the phase-20b remote-Ollama toggle so a
        # disabled remote agent disappears from the picker without the
        # registry having to be rebuilt.
        "agents": enabled_agents(db),
    }
    if composer:
        ctx["default_tool_states"] = _default_tool_states()
    return ctx
