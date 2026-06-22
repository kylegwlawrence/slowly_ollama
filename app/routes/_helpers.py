"""Shared helpers used by multiple route sub-modules.

Lives alongside the sub-routers so they can each import the per-render
context and the host-overrides resolver from
one place rather than reaching across to a sibling sub-module.
"""

import sqlite3

from app import queries, tools
from app import rag_servers as _rag_servers
from app.hosts import enabled_hosts, get_host
from app.templates import templates


def _sidebar_reference_oob(db: sqlite3.Connection) -> str:
    """Render the sidebar reference section as an OOB-swappable fragment.

    Used by the settings-page RAG server CRUD routes so a freshly
    added/edited/deleted server appears in the always-visible sidebar
    "Sources" list immediately, without a full browser reload. The
    rendered ``<section id="sidebar-reference">`` carries
    ``hx-swap-oob="true"`` so HTMX matches it by id and replaces it in
    place, regardless of the response's primary swap target.

    Args:
        db: Open SQLite connection — feeds the current server/tool lists.

    Returns:
        The rendered HTML fragment, ready to append to a route response.
    """
    return templates.get_template("_sidebar_reference.html").render(
        oob=True, **_sidebar_reference_context(db)
    )


def _sidebar_reference_context(db: sqlite3.Connection) -> dict:
    """Build the context for the always-visible sidebar reference lists (phase 24).

    The sidebar shows two chat-independent reference lists below the projects
    list: every configured RAG server and every registered tool. This replaced
    the chat-gated Sources health panel, so there is no active-chat gating and
    no health probing — just "what the app has".

    Returns the keys consumed by ``_sidebar_reference.html``:
      ``sidebar_rag_servers``: list[RagServer] — every configured server.
      ``sidebar_tools``: list[ToolSpec]        — every tool, name-sorted.

    Both are cheap to gather (one SQL query + an in-memory dict), so this runs
    on every full-page render without a cache.
    """
    return {
        "sidebar_rag_servers": _rag_servers.list_servers(db),
        "sidebar_tools": sorted(tools.TOOLS.values(), key=lambda s: s.name),
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


def _resolve_active_host(
    conversation: queries.Conversation, db: sqlite3.Connection
):
    """Return the effective HostSpec for a conversation, honoring the toggle.

    Wraps :func:`app.hosts.get_host` so callers that render the chat
    header (model chip, host indicator, unload button) all see the same
    "what's actually going to run" view: a chat with
    ``active_host="host2"`` resolves to ``None`` when the phase-20b
    Remote Ollama toggle is off, so the indicator shows the primary host's
    pinned model instead of the now-disabled second host.

    Args:
        conversation: The chat whose ``active_host`` to resolve.
        db: Open SQLite connection — the toggle lives in ``app_settings``.

    Returns:
        The resolved ``HostSpec``, or ``None`` for the primary host (no
        selection, unknown name, OR a second-host spec while the toggle is
        off).
    """
    spec = get_host(conversation.active_host)
    if spec is not None and spec.ollama_host is not None:
        if not queries.get_remote_ollama_enabled(db):
            return None
    return spec


def _effective_model(
    conversation: queries.Conversation,
    spec,
    db: sqlite3.Connection,
) -> str:
    """Return the model a conversation will actually run on, given its host.

    The single rule every header-render + generation site shares:

    - Primary host (``spec is None``): the chat's pinned ``conversation.model``.
    - A non-primary host: the chat's remembered model for that host
      (``chat_host_models``), or the host's ``default_model`` when the chat has
      none.

    Args:
        conversation: The chat whose effective model to resolve.
        spec: The resolved host ``HostSpec`` (from :func:`_resolve_active_host`),
            or ``None`` for the primary host.
        db: Open SQLite connection — non-primary models live in
            ``chat_host_models``.

    Returns:
        The Ollama model tag to use for this chat on its current host.
    """
    if spec is None:
        return conversation.model
    return (
        queries.get_chat_host_model(db, conversation.id, spec.name)
        or spec.model
    )


def _resolve_think(conversation: queries.Conversation) -> bool | None:
    """Map a chat's ``think_mode`` to Ollama's ``think`` flag (phase 25).

    Args:
        conversation: The chat whose ``think_mode`` to resolve.

    Returns:
        ``False`` when ``think_mode == 'off'`` (suppress the reasoning phase;
        safe on any model). ``None`` otherwise (``'default'`` — omit the key
        so the model decides). Never returns ``True``: the v1 toggle only
        suppresses thinking, so we never risk a ``think=true`` 400 on a
        non-thinking model.
    """
    return False if conversation.think_mode == "off" else None


def _host_overrides(
    conversation: queries.Conversation, db: sqlite3.Connection
) -> dict:
    """Resolve a conversation's selected Ollama host into ``start_generation`` kwargs.

    The per-chat picker selects a host, stored in
    ``conversation.active_host``: NULL/empty (or an unknown/removed name)
    means the primary host ("host1"); a known name (e.g. ``"host2"``) means
    that registered second host.

    The primary host returns the chat's pinned model with ``ollama_host=None``
    (local); a selected second host returns
    its ``ollama_host`` and the chat's per-host model (the ``chat_host_models``
    row for that host, falling back to the host spec's ``default_model``), but
    is otherwise IDENTICAL — both run the plain generation path, so the project
    system prompt and the full tool registry apply on either host; only the
    machine and the model differ. Both branches carry the chat's resolved
    ``think`` flag (phase 25, via :func:`_resolve_think`).

    Phase 20b gating still applies: when a selected host's spec has a non-None
    ``ollama_host`` AND the app-wide ``remote_ollama_enabled`` toggle is off,
    ``_resolve_active_host`` returns None and we fall back to the primary host
    — chats with ``active_host="host2"`` then run plain on the chat's pinned
    local model, no data loss, regardless of whether the second host is up.
    """
    spec = _resolve_active_host(conversation, db)
    if spec is None:
        return {
            "model": conversation.model,
            "think": _resolve_think(conversation),
            "ollama_host": None,
        }
    # A selected non-primary host uses the chat's per-host model when set,
    # falling back to the host spec's default_model.
    return {
        "model": _effective_model(conversation, spec, db),
        "think": _resolve_think(conversation),
        "ollama_host": spec.ollama_host,
    }


def _composer_host_context(
    db: sqlite3.Connection, project: queries.Project
) -> dict:
    """Initial-host + model-default hooks for the composer's single model dropdown.

    The composer renders ONE model dropdown that re-fetches the selected host's
    models (instead of one dropdown per host). On first render it loads the
    *initial* host's models and pre-selects that host's default model:

    - primary initial host → the project (or global) default model;
    - a non-primary default host → that host's configured ``default_model``.

    ``primary_default_model`` is the picker's primary option's
    ``data-default-model`` — app.js reads it when the user switches BACK to the
    primary host so the dropdown re-selects the right default.

    Args:
        db: Open SQLite connection (for the global default model).
        project: The project the composer is scoped to.

    Returns:
        ``composer_initial_host`` (host name or ""), ``composer_initial_model``
        (the model dropdown's initial ``data-default``), and
        ``primary_default_model``.
    """
    primary_default_model = (
        project.default_model or queries.get_default_model(db) or ""
    )
    initial_host = project.default_agent or ""
    initial_model_default = primary_default_model
    if initial_host:
        spec = get_host(initial_host)
        if spec is not None:
            initial_model_default = spec.model
        else:
            # Project default points at a host that's no longer configured →
            # fall back to the primary host.
            initial_host = ""
    return {
        "composer_initial_host": initial_host,
        "composer_initial_model": initial_model_default,
        "primary_default_model": primary_default_model,
    }


def _project_context(
    db: sqlite3.Connection,
    *,
    composer: bool = False,
) -> dict:
    """Build the shared context for project page renders.

    Pulls together global defaults the composer / chat panel need —
    default temperature, default tool cap, host registry. Centralized here
    so every project endpoint renders with the same shape.

    Args:
        db: Open SQLite connection.
        composer: Accepted for call-site symmetry; no longer changes the
            returned context (the composer's per-chat tool chips were removed
            in phase 23).
    """
    return {
        "default_temperature": queries.get_default_temperature(db),
        "default_tool_iteration_cap": queries.get_default_tool_iteration_cap(db),
        "global_default_model": queries.get_default_model(db),
        # enabled_hosts respects the phase-20b remote-Ollama toggle so a
        # disabled remote host disappears from the picker without the
        # registry having to be rebuilt.
        "hosts": enabled_hosts(db),
    }
