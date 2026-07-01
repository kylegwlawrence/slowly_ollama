"""Shared helpers used by multiple route sub-modules.

Lives alongside the sub-routers so they share one import source for the
per-render context and the host-overrides resolver.
"""

import sqlite3

from app import queries, tools
from app import rag_servers as _rag_servers
from app.hosts import enabled_hosts, get_host, get_primary_host, UnknownHostError
from app.templates import templates


def _sidebar_reference_oob(db: sqlite3.Connection) -> str:
    """Render the sidebar reference section as an OOB-swappable fragment.

    Lets the settings-page RAG server CRUD routes refresh the sidebar
    "Sources" list without a full reload. The ``<section
    id="sidebar-reference">`` carries ``hx-swap-oob="true"`` so HTMX
    replaces it by id regardless of the response's primary swap target.

    Args:
        db: Open SQLite connection.

    Returns:
        The rendered HTML fragment.
    """
    return templates.get_template("_sidebar_reference.html").render(
        oob=True, **_sidebar_reference_context(db)
    )


def _sidebar_reference_context(db: sqlite3.Connection) -> dict:
    """Build the context for the always-visible sidebar reference lists.

    Two chat-independent lists below the projects list: every configured
    RAG server and every registered tool. No active-chat gating, no health
    probing — just "what the app has". Both are cheap (one SQL query + an
    in-memory dict), so this runs on every full-page render without a cache.

    Returns the keys consumed by ``_sidebar_reference.html``:
      ``sidebar_rag_servers``: list[RagServer] — every configured server.
      ``sidebar_tools``: list[ToolSpec]        — every tool, name-sorted.
    """
    return {
        "sidebar_rag_servers": _rag_servers.list_servers(db),
        "sidebar_tools": sorted(tools.TOOLS.values(), key=lambda s: s.name),
    }


def _placeholder_name(content: str) -> str:
    """Derive a sidebar-friendly placeholder name from a user message.

    Gives a new chat an identifiable sidebar entry on creation instead of
    a generic "New chat"; the auto-titler may replace it later.

    Args:
        content: The user's first message. Collapsed to the first non-empty
            line and trimmed. The 40-char cap fits a 280px-wide sidebar.

    Returns:
        Up to 40 chars of the first non-empty line, or ``"New chat"`` when
        ``content`` is empty/whitespace-only (defensive — Form() requires
        content).
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

    Returns the owning project's ``num_ctx`` when set, otherwise the global
    default. Falls back to the global default when the project can't be
    resolved (defensive — every chat should have a project).
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

    Gives every chat-header render (model chip, host indicator, unload
    button) the same "what's actually going to run" view. A chat selecting a
    remote host resolves back to the *primary* host when the Remote Ollama
    toggle is off, so the indicator shows the primary host's pinned model.

    ``conversation.active_host`` is reconciled at startup, so ``get_host``
    won't raise — a stored value is always NULL or a registered host.

    Args:
        conversation: The chat whose ``active_host`` to resolve.
        db: Open SQLite connection — the toggle lives in ``app_settings``.

    Returns:
        The resolved ``HostSpec``: the primary host for no selection or a
        toggle-disabled remote host, otherwise the selected host.
    """
    spec = get_host(conversation.active_host)
    if not spec.is_primary and spec.ollama_host is not None:
        if not queries.get_remote_ollama_enabled(db):
            return get_primary_host()
    return spec


def _effective_model(
    conversation: queries.Conversation,
    spec,
    db: sqlite3.Connection,
) -> str:
    """Return the model a conversation will actually run on, given its host.

    The shared rule for every header-render + generation site:

    - Primary host: the chat's pinned ``conversation.model``.
    - Non-primary host: the chat's remembered model for that host
      (``chat_host_models``), or the host's ``default_model`` when unset.

    Args:
        conversation: The chat whose effective model to resolve.
        spec: The resolved host ``HostSpec`` (from :func:`_resolve_active_host`).
        db: Open SQLite connection — non-primary models live in
            ``chat_host_models``.

    Returns:
        The Ollama model tag to use for this chat on its current host.
    """
    if spec.is_primary:
        return conversation.model
    return (
        queries.get_chat_host_model(db, conversation.id, spec.name)
        or spec.model
    )


def _resolve_think(conversation: queries.Conversation) -> bool | None:
    """Map a chat's ``think_mode`` to Ollama's ``think`` flag.

    Args:
        conversation: The chat whose ``think_mode`` to resolve.

    Returns:
        ``False`` when ``think_mode == 'off'`` (suppress reasoning; safe on
        any model), else ``None`` (omit the key so the model decides). Never
        ``True`` — the toggle only suppresses thinking, so we never risk a
        ``think=true`` 400 on a non-thinking model.
    """
    return False if conversation.think_mode == "off" else None


def _host_overrides(
    conversation: queries.Conversation, db: sqlite3.Connection
) -> dict:
    """Resolve a conversation's selected Ollama host into ``start_generation`` kwargs.

    ``conversation.active_host`` selects the host: NULL/empty (or an
    unknown/removed name) means the primary host; a known name means that
    registered host. The primary returns the chat's pinned model with
    ``ollama_host=None`` (local); a non-primary returns its ``ollama_host``
    and the chat's per-host model (falling back to the spec's
    ``default_model``). Both are otherwise IDENTICAL — same plain generation
    path, project system prompt, and full tool registry; only the machine and
    model differ. Both carry the resolved ``think`` flag.

    Toggle gating still applies: ``_resolve_active_host`` resolves a
    toggle-disabled remote host back to the primary, so the chat runs on its
    pinned local model with no data loss.
    """
    spec = _resolve_active_host(conversation, db)
    if spec.is_primary:
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

    The composer renders ONE model dropdown that re-fetches the selected
    host's models. On first render it loads the initial host's models and
    pre-selects that host's default:

    - primary initial host → the project (or global) default model;
    - a non-primary default host → that host's ``default_model``.

    ``primary_default_model`` is read by app.js when the user switches BACK
    to the primary host, to re-select the right default.

    Args:
        db: Open SQLite connection.
        project: The project the composer is scoped to.

    Returns:
        ``composer_initial_host`` (host name or ""), ``composer_initial_model``
        (the dropdown's initial ``data-default``), and
        ``primary_default_model``.
    """
    primary_default_model = (
        project.default_model or queries.get_default_model(db) or ""
    )
    initial_host = project.default_agent or ""
    initial_model_default = primary_default_model
    if initial_host:
        # project.default_agent isn't reconciled at startup (only
        # conversations are), so a project pinned to a since-removed host can
        # hold a stale name. Catch it and fall back to the primary host.
        try:
            initial_model_default = get_host(initial_host).model
        except UnknownHostError:
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

    Gathers the global defaults the composer / chat panel need — default
    temperature, default tool cap, host registry — so every project endpoint
    renders with the same shape.

    Args:
        db: Open SQLite connection.
        composer: Accepted for call-site symmetry; no longer changes the
            returned context.
    """
    return {
        "default_temperature": queries.get_default_temperature(db),
        "default_tool_iteration_cap": queries.get_default_tool_iteration_cap(db),
        "global_default_model": queries.get_default_model(db),
        # enabled_hosts respects the remote-Ollama toggle so a disabled
        # remote host disappears from the picker without rebuilding the
        # registry.
        "hosts": enabled_hosts(db),
        # Reusable agents (personas) for the per-chat picker in _chat_panel.
        # Global (not project-scoped); the same list every project render.
        "agents": queries.list_agents(db),
    }
