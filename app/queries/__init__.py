"""Dataclasses for the persisted rows and the query functions that read /
write them.

Originally a single ``app/queries.py`` module; split into submodules at the
1500-line mark so each table's CRUD lives near its dataclass. The package
``__init__`` re-exports the full public surface, so ``from app.queries
import X`` continues to work for every X the old module exposed.

Submodules:
    :mod:`app.queries._models` — shared dataclasses (Conversation, Message,
        Project, ChatToolState, ChatRagState) + Role literal + _UNSET sentinel.
    :mod:`app.queries.conversations` — CRUD for the conversations table.
    :mod:`app.queries.messages` — CRUD for the messages table + token-count
        helpers.
    :mod:`app.queries.projects` — CRUD for the projects table + slugifier.
    :mod:`app.queries.settings` — app_settings get/set + global default-*
        helpers (temperature, model, tool cap, num_ctx).
    :mod:`app.queries.chat_state` — per-chat tool + RAG-server chip state.

Each function takes a ``sqlite3.Connection`` and wraps writes in
``with conn:`` so a partial update never lands if anything raises mid-way.
Timestamps are stored as ISO 8601 TEXT in UTC and converted to/from
``datetime`` at the boundary so callers work with proper datetime values
instead of strings.
"""

from app.queries._models import (
    ChatRagState,
    ChatToolState,
    Conversation,
    Message,
    Project,
    Role,
    _Unset,
    _UNSET,
)
from app.queries.chat_state import (
    get_chat_rag_states,
    get_chat_tool_states,
    get_enabled_rag_server_names,
    get_enabled_tool_names,
    seed_chat_rag_servers,
    seed_chat_tools,
    toggle_chat_rag_server,
    toggle_chat_tool,
)
from app.queries.conversations import (
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations,
    list_conversations_in_project,
    rename_conversation,
    set_active_agent,
    set_conversation_temperature,
    set_conversation_tool_iteration_cap,
    set_name_auto,
)
from app.queries.messages import (
    append_message,
    archive_messages_before,
    count_archived_messages,
    count_assistant_messages,
    list_active_messages,
    list_messages,
    replace_last_assistant_message,
)
from app.queries.projects import (
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
    "ChatRagState",
    "ChatToolState",
    "Conversation",
    "Message",
    "Project",
    "Role",
    "_UNSET",
    "_Unset",
    # Conversations
    "create_conversation",
    "delete_conversation",
    "get_conversation",
    "list_conversations",
    "list_conversations_in_project",
    "rename_conversation",
    "set_active_agent",
    "set_conversation_temperature",
    "set_conversation_tool_iteration_cap",
    "set_name_auto",
    # Messages
    "append_message",
    "archive_messages_before",
    "count_archived_messages",
    "count_assistant_messages",
    "list_active_messages",
    "list_messages",
    "replace_last_assistant_message",
    # Projects
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
    # Chat state (per-chat chips)
    "get_chat_rag_states",
    "get_chat_tool_states",
    "get_enabled_rag_server_names",
    "get_enabled_tool_names",
    "seed_chat_rag_servers",
    "seed_chat_tools",
    "toggle_chat_rag_server",
    "toggle_chat_tool",
]
