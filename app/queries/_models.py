"""Dataclasses + the Role literal shared across the queries package.

Centralized here so the per-table CRUD submodules can import the shapes
they need without circular imports.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


# Role is enforced in Python, not the schema: the CHECK constraint was
# dropped when tool_call/tool_result were added and SQLite can't ALTER a
# CHECK. The Literal lets a type checker catch wrong-role bugs early.
Role = Literal[
    "user",
    "assistant",
    "tool_call",
    "tool_result",
    # Synthetic row from the manual-compact endpoint. Its `content` is the
    # model-generated summary of earlier turns, sent to Ollama as a `system`
    # message (see app.generation.build_history_payload). At most one active
    # (archived_at IS NULL) summary exists per chat; re-compacting archives
    # the prior one.
    "summary",
]


@dataclass(frozen=True)
class Conversation:
    """One row of the `conversations` table.

    Attributes:
        id: Auto-assigned primary key.
        name: Human-readable label shown in the sidebar.
        model: The primary-host Ollama model (e.g. "llama3:latest"). A
            non-primary host's per-chat model lives in ``chat_host_models``,
            not here — see ``app.queries.chat_hosts``.
        name_locked: When True, the auto-titler leaves `name` alone. Flipped
            by `rename_conversation` so a manual rename beats a later auto
            title refresh.
        created_at: When the row was inserted (UTC).
        updated_at: When the row was last touched (rename, append message,
            replace last assistant message). The sidebar's sort key, so
            active chats float up.
        active_host: Selected Ollama host (a key in `app.hosts.HOSTS`, e.g.
            "host2"), or None for the primary host. Persisted so the picker
            survives reloads.
        project_id: Owning project. NOT NULL: the migration assigns legacy
            chats to the Default project before enforcing the FK.
        think_mode: Thinking lever. 'default' omits Ollama's ``think`` key
            (model decides); 'off' sends ``think=false`` to suppress a
            reasoning model's <think> phase.
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
    active_host: str | None = None
    think_mode: str = "default"


@dataclass(frozen=True)
class Project:
    """One row of the ``projects`` table.

    Attributes:
        id: Auto-assigned primary key.
        name: Display name, unique across the table.
        description: Free-text description (may be empty).
        workspace_subdir: Path segment under ``FILE_TOOL_ROOT`` — the
            workspace lives at ``FILE_TOOL_ROOT/<subdir>/``. Slugified from
            ``name`` at create time; never edited.
        default_model: Pre-fill for the model dropdown on new chats.
            ``None`` = use the global default.
        default_agent: Pre-selection for the agent dropdown on new chats.
            ``None`` = Normal (no agent).
        num_ctx: Per-project override for Ollama's ``num_ctx`` (token context
            window). ``None`` inherits the global default. Applied per turn,
            so a change takes effect on the next message in any chat here.
        system_prompt: Prepended to Normal-chat turns; ``""`` = none, capped
            at SYSTEM_PROMPT_MAX_CHARS. Ignored on invoked-agent turns (the agent's prompt
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
    """Sentinel for "argument intentionally omitted" in updaters.

    Lets ``update_project`` distinguish "kwarg not passed (leave alone)"
    from "kwarg passed as ``None`` (set SQL NULL)" — clearing a field from
    the UI must persist as NULL, not be silently ignored.
    """


_UNSET = _Unset()


@dataclass(frozen=True)
class Message:
    """One row of the `messages` table.

    Attributes:
        id: Auto-assigned primary key.
        conversation_id: Foreign key into `conversations`.
        role: One of the `Role` literal values (validated in Python).
        content: The message text.
        created_at: When the row was inserted (UTC). A regenerated assistant
            message keeps its original timestamp so message order is stable.
    """

    id: int
    conversation_id: int
    role: Role
    content: str
    created_at: datetime
    # Per-turn token counts from Ollama's final stream chunk. NULL on
    # non-assistant rows, rows older than this column, and responses where
    # Ollama reported no counts (e.g. full prompt-cache hit). See
    # `app.ollama.ChatChunk` for interpretation.
    prompt_tokens: int | None = None
    eval_tokens: int | None = None
    # Wall-clock generation time for this assistant turn, in milliseconds
    # (producer start → done, spanning the whole tool loop + stream). NULL on
    # non-assistant rows, pre-existing rows, and turns that errored before
    # completing. Rendered under the token counts as a human-readable string.
    duration_ms: int | None = None
    # A thinking model's streamed reasoning for this assistant turn, or NULL.
    # Populated by `append_message` / `replace_last_assistant_message` from
    # the producer's accumulated `message.thinking` chunks; NULL on
    # non-assistant rows, pre-existing rows, and non-reasoning turns.
    # Rebuilt into a collapsed card above the bubble on historic render.
    thinking: str | None = None
    # Set by `archive_messages_before` to hide the row from the prompt
    # (manual compaction); NULL = active. Never written by `append_message`.
    archived_at: datetime | None = None
