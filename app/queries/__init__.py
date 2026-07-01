"""Row dataclasses and the query functions that read / write them.

Split into per-table submodules; this ``__init__`` re-exports the full
public surface so ``from app.queries import X`` works for every X.

Submodules:
    :mod:`app.queries._models` — shared dataclasses + Role literal + sentinel.
    :mod:`app.queries.agents` — reusable-agent (persona) CRUD.
    :mod:`app.queries.conversations` — conversations CRUD.
    :mod:`app.queries.messages` — messages CRUD + token-count helpers.
    :mod:`app.queries.projects` — projects CRUD + slugifier.
    :mod:`app.queries.settings` — app_settings + global default-* helpers.
    :mod:`app.queries.chat_hosts` — per-chat model for each non-primary host.

Each function takes a ``sqlite3.Connection`` and wraps writes in
``with conn:`` so a partial update never lands. Timestamps are stored as
ISO 8601 UTC TEXT and converted to/from ``datetime`` at the boundary.
"""

from app.queries._models import (
    Agent,
    Conversation,
    Message,
    Project,
    Role,
    _Unset,
    _UNSET,
)
from app.queries.agents import (
    AGENT_NAME_MAX_CHARS,
    create_agent,
    delete_agent,
    get_agent,
    get_agent_for_conversation,
    list_agents,
    update_agent,
)
from app.queries.chat_hosts import (
    get_chat_host_model,
    set_chat_host_model,
)
from app.queries.conversations import (
    clear_unknown_active_hosts,
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations,
    list_conversations_in_project,
    rename_conversation,
    set_active_host,
    set_conversation_agent,
    set_conversation_temperature,
    set_conversation_think_mode,
    set_conversation_tool_iteration_cap,
    set_name_auto,
)
from app.queries.messages import (
    append_message,
    archive_messages_before,
    count_assistant_messages,
    list_active_messages,
    list_messages,
    replace_last_assistant_message,
)
from app.queries.projects import (
    SYSTEM_PROMPT_MAX_CHARS,
    count_projects,
    create_project,
    delete_project,
    get_project,
    get_project_for_conversation,
    list_projects,
    slugify_project_name,
    update_project,
)
from app.queries.settings import (
    NUM_CTX_MAX,
    NUM_CTX_MIN,
    clamp_num_ctx,
    get_default_model,
    get_default_num_ctx,
    get_default_temperature,
    get_default_tool_iteration_cap,
    get_remote_ollama_enabled,
    get_setting,
    resolve_num_ctx_for_project,
    set_default_model,
    set_default_num_ctx,
    set_default_temperature,
    set_default_tool_iteration_cap,
    set_remote_ollama_enabled,
    set_setting,
)

__all__ = [
    # Models
    "Agent",
    "Conversation",
    "Message",
    "Project",
    "Role",
    "_UNSET",
    "_Unset",
    # Agents (reusable personas)
    "AGENT_NAME_MAX_CHARS",
    "create_agent",
    "delete_agent",
    "get_agent",
    "get_agent_for_conversation",
    "list_agents",
    "update_agent",
    # Conversations
    "clear_unknown_active_hosts",
    "create_conversation",
    "delete_conversation",
    "get_conversation",
    "list_conversations",
    "list_conversations_in_project",
    "rename_conversation",
    "set_active_host",
    "set_conversation_agent",
    "set_conversation_temperature",
    "set_conversation_think_mode",
    "set_conversation_tool_iteration_cap",
    "set_name_auto",
    # Messages
    "append_message",
    "archive_messages_before",
    "count_assistant_messages",
    "list_active_messages",
    "list_messages",
    "replace_last_assistant_message",
    # Projects
    "SYSTEM_PROMPT_MAX_CHARS",
    "count_projects",
    "create_project",
    "delete_project",
    "get_project",
    "get_project_for_conversation",
    "list_projects",
    "slugify_project_name",
    "update_project",
    # Settings
    "NUM_CTX_MAX",
    "NUM_CTX_MIN",
    "clamp_num_ctx",
    "get_default_model",
    "get_default_num_ctx",
    "get_default_temperature",
    "get_default_tool_iteration_cap",
    "get_remote_ollama_enabled",
    "get_setting",
    "resolve_num_ctx_for_project",
    "set_default_model",
    "set_default_num_ctx",
    "set_default_temperature",
    "set_default_tool_iteration_cap",
    "set_remote_ollama_enabled",
    "set_setting",
    # Per-chat host models
    "get_chat_host_model",
    "set_chat_host_model",
]
