"""Dataclasses + the Role literal shared across the queries package.

Lives in one place so the per-table CRUD submodules
(``conversations``, ``messages``, ``projects``, ``chat_state``) can each
import the shapes they need without circular imports.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


# Role values are constrained at the type level here. The schema-level
# CHECK was dropped in phase 12a (12a added tool_call/tool_result) and
# SQLite can't ALTER CHECK constraints, so we enforce here in Python
# instead. The Literal alias documents intent and lets a type checker
# catch wrong-role bugs before they hit SQLite.
Role = Literal[
    "user",
    "assistant",
    "tool_call",
    "tool_result",
]


@dataclass(frozen=True)
class Conversation:
    """One row of the `conversations` table.

    Attributes:
        id: Auto-assigned primary key.
        name: Human-readable label shown in the sidebar.
        model: Ollama model identifier (e.g. "llama3:latest").
        name_locked: When True, the auto-titler must leave `name` alone.
            Flipped to True by `rename_conversation` so a manual rename
            always beats a later automated title refresh.
        created_at: When the row was first inserted (UTC).
        updated_at: When the row was last touched — bumped by rename, by
            appending a message, or by replacing the last assistant message.
            Used as the sort key for the sidebar so active chats float up.
        active_agent: Name of the user-invoked agent currently active for this
            chat (a key in `app.agents.AGENTS`), or None for the default
            "Normal" plain-chat behavior. Persisted so the picker + indicator
            survive reloads.
        project_id: The project this chat belongs to (phase 17). NOT NULL on
            the schema side: the migration assigns every legacy chat to the
            Default project before enforcing the FK.
    """

    id: int
    name: str
    model: str
    name_locked: bool
    temperature: float
    tool_iteration_cap: int
    project_id: int
    created_at: datetime
    updated_at: datetime
    active_agent: str | None = None


@dataclass(frozen=True)
class Project:
    """One row of the ``projects`` table (phase 17).

    Attributes:
        id: Auto-assigned primary key.
        name: Human-readable display name, unique across the projects table.
        description: Free-text description (may be empty).
        workspace_subdir: Path segment under ``FILE_TOOL_ROOT`` — the
            project's workspace lives at ``FILE_TOOL_ROOT/<subdir>/``.
            Slugified from ``name`` at create time; never edited.
        default_model: Pre-fill for the model dropdown on new chats in this
            project. ``None`` means no project default (use the global one).
        default_agent: Pre-selection for the agent dropdown on new chats.
            ``None`` means Normal (no agent).
        num_ctx: Per-project override for Ollama's ``num_ctx`` (context
            window in tokens). ``None`` means inherit the global default
            from ``app_settings``. Applied per turn, not seeded onto chats,
            so changing it takes effect on the next message in any chat
            belonging to this project.
        system_prompt: Per-project system prompt prepended to Normal-chat
            turns. Empty string = none. Capped at 200 chars at the route
            layer. Ignored on invoked-agent turns (the agent's own prompt
            wins).
        created_at, updated_at: ISO 8601 UTC timestamps.
    """

    id: int
    name: str
    description: str
    workspace_subdir: str
    default_model: str | None
    default_agent: str | None
    num_ctx: int | None
    created_at: datetime
    updated_at: datetime
    system_prompt: str = ""


class _Unset:
    """Sentinel marker for "argument intentionally omitted" in updaters.

    Allows ``update_project`` to distinguish "this kwarg was not passed
    (leave the field alone)" from "this kwarg was passed as ``None`` (set
    the field to SQL NULL)". A plain ``None`` default cannot tell the two
    apart, but clearing ``default_model`` / ``default_agent`` from the UI
    must persist as NULL, not be silently ignored.
    """


_UNSET = _Unset()


@dataclass(frozen=True)
class Message:
    """One row of the `messages` table.

    Attributes:
        id: Auto-assigned primary key.
        conversation_id: Foreign key into `conversations`.
        role: One of the values in the `Role` literal alias above.
            Phase 12a widened this to include "tool_call" and
            "tool_result"; validation now lives in Python (the schema
            CHECK was dropped in the same phase).
        content: The message text.
        created_at: When the row was first inserted (UTC). For a regenerated
            assistant message the original timestamp is preserved so message
            order is unchanged.
    """

    id: int
    conversation_id: int
    role: Role
    content: str
    created_at: datetime
    # Per-turn token counts reported by Ollama on the final stream chunk
    # of an assistant message. NULL on non-assistant rows, on assistant
    # rows persisted before this column existed, and on responses where
    # Ollama didn't report counts (e.g. full prompt-cache hit). See
    # `app.ollama.ChatChunk` for the source-of-truth interpretation.
    prompt_tokens: int | None = None
    eval_tokens: int | None = None


@dataclass(frozen=True)
class ChatToolState:
    """Enabled/disabled state of one tool for one conversation.

    Attributes:
        tool_name: Registered name of the tool (matches TOOLS key).
        enabled: True when the tool is active for this conversation.
    """

    tool_name: str
    enabled: bool


@dataclass(frozen=True)
class ChatRagState:
    """Enabled/disabled state of one RAG server for one conversation.

    Attributes:
        server_name: Unique name from rag_servers.name (e.g. ``"arxiv"``).
        enabled: True when this server's chip is toggled on for the chat.
    """

    server_name: str
    enabled: bool
