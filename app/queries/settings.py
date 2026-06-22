"""Settings stored in the ``app_settings`` table.

Holds the global default values that new chats inherit (temperature,
model, tool-cap, num_ctx) plus generic ``get_setting`` / ``set_setting``
helpers other modules use for one-shot flags (e.g. the workspace v2
migration marker).
"""

import sqlite3


def get_setting(
    conn: sqlite3.Connection, key: str, default: str | None = None
) -> str | None:
    """Read a single app_settings row by key.

    Args:
        conn: Open SQLite connection.
        key: Setting key (e.g. ``"default_temperature"``).
        default: Returned when no row exists for the key.

    Returns:
        The stored value as a string, or ``default`` when the key
        hasn't been set.
    """
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?;", (key,)
    ).fetchone()
    return row["value"] if row is not None else default


def set_setting(
    conn: sqlite3.Connection, key: str, value: str
) -> None:
    """Upsert one app_settings row.

    Wraps the write in ``with conn:`` so the upsert lands atomically.

    Args:
        conn: Open SQLite connection.
        key: Setting key.
        value: Setting value as a string.
    """
    with conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
            (key, value),
        )


_DEFAULT_TEMPERATURE_KEY = "default_temperature"
_DEFAULT_TEMPERATURE_FALLBACK = 0.2


def get_default_temperature(conn: sqlite3.Connection) -> float:
    """Return the global default sampling temperature for new chats.

    Default (no row): ``0.2``. The stored value is clamped to the
    [0.0, 2.0] range Ollama accepts; a malformed row (non-numeric,
    written by a hand-crafted request) falls back to ``0.2`` rather
    than raising, so a corrupt setting can never break chat creation.

    Args:
        conn: Open SQLite connection.
    """
    raw = get_setting(conn, _DEFAULT_TEMPERATURE_KEY, default=None)
    if raw is None:
        return _DEFAULT_TEMPERATURE_FALLBACK
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TEMPERATURE_FALLBACK
    return max(0.0, min(2.0, value))


def set_default_temperature(
    conn: sqlite3.Connection, temperature: float
) -> None:
    """Persist the global default sampling temperature for new chats.

    Clamps to [0.0, 2.0] before storing so an out-of-range value can't
    be read back later. Stored as a string (the app_settings value
    column is text).

    Args:
        conn: Open SQLite connection.
        temperature: New default temperature (clamped to 0.0–2.0).
    """
    clamped = max(0.0, min(2.0, float(temperature)))
    set_setting(conn, _DEFAULT_TEMPERATURE_KEY, str(clamped))


_DEFAULT_MODEL_KEY = "default_model"


def get_default_model(conn: sqlite3.Connection) -> str | None:
    """Return the global default model for new chats, or None if unset.

    Args:
        conn: Open SQLite connection.
    """
    return get_setting(conn, _DEFAULT_MODEL_KEY, default=None)


def set_default_model(conn: sqlite3.Connection, model: str | None) -> None:
    """Persist the global default model for new chats.

    Passing ``None`` or an empty string clears the setting so no
    model is pre-selected by the global default.

    Args:
        conn: Open SQLite connection.
        model: Ollama model identifier (e.g. ``"granite4.1:8b"``), or
            ``None`` / empty string to clear.
    """
    if model:
        set_setting(conn, _DEFAULT_MODEL_KEY, model)
    else:
        with conn:
            conn.execute(
                "DELETE FROM app_settings WHERE key = ?;",
                (_DEFAULT_MODEL_KEY,),
            )


_DEFAULT_TOOL_ITERATION_CAP_KEY = "default_tool_iteration_cap"
_DEFAULT_TOOL_ITERATION_CAP_FALLBACK = 5


def get_default_tool_iteration_cap(conn: sqlite3.Connection) -> int:
    """Return the global default per-turn tool-iteration cap for new chats.

    Default (no row): ``5``. The stored value is clamped to the [1, 10]
    range the app enforces; a malformed row falls back to ``5`` rather
    than raising, so a corrupt setting can never break chat creation.

    Args:
        conn: Open SQLite connection.
    """
    raw = get_setting(conn, _DEFAULT_TOOL_ITERATION_CAP_KEY, default=None)
    if raw is None:
        return _DEFAULT_TOOL_ITERATION_CAP_FALLBACK
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TOOL_ITERATION_CAP_FALLBACK
    return max(1, min(10, value))


def set_default_tool_iteration_cap(
    conn: sqlite3.Connection, tool_iteration_cap: int
) -> None:
    """Persist the global default per-turn tool-iteration cap for new chats.

    Clamps to [1, 10] before storing so an out-of-range value can't be
    read back later. Stored as a string (the app_settings value column
    is text).

    Args:
        conn: Open SQLite connection.
        tool_iteration_cap: New default cap (clamped to 1–10).
    """
    clamped = max(1, min(10, int(tool_iteration_cap)))
    set_setting(conn, _DEFAULT_TOOL_ITERATION_CAP_KEY, str(clamped))


# Ollama's own default for `num_ctx` is 2048 — far too small for real
# conversations. 16384 matches what most local 7-13B models comfortably
# fit and what tool-using sessions typically need. NUM_CTX_MIN/MAX bound
# the clamp on read and write: 512 is below any usable chat context, and
# 1_048_576 (1M) is a future-proof ceiling well above any current model.
_DEFAULT_NUM_CTX_KEY = "default_num_ctx"
_DEFAULT_NUM_CTX_FALLBACK = 16384
NUM_CTX_MIN = 512
NUM_CTX_MAX = 1_048_576


def clamp_num_ctx(num_ctx: int) -> int:
    """Clamp a num_ctx value to the [NUM_CTX_MIN, NUM_CTX_MAX] range."""
    return max(NUM_CTX_MIN, min(NUM_CTX_MAX, int(num_ctx)))


def get_default_num_ctx(conn: sqlite3.Connection) -> int:
    """Return the global default Ollama context window for new chats.

    Default (no row): ``16384`` (see ``_DEFAULT_NUM_CTX_FALLBACK``). The
    stored value is clamped to the [NUM_CTX_MIN, NUM_CTX_MAX] range; a
    malformed row falls back to the default rather than raising, so a
    corrupt setting can never break chat creation.

    Args:
        conn: Open SQLite connection.
    """
    raw = get_setting(conn, _DEFAULT_NUM_CTX_KEY, default=None)
    if raw is None:
        return _DEFAULT_NUM_CTX_FALLBACK
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_NUM_CTX_FALLBACK
    return clamp_num_ctx(value)


def set_default_num_ctx(conn: sqlite3.Connection, num_ctx: int) -> None:
    """Persist the global default Ollama context window for new chats.

    Clamps to [NUM_CTX_MIN, NUM_CTX_MAX] before storing so an out-of-
    range value can't be read back later. Stored as a string (the
    app_settings value column is text).

    Args:
        conn: Open SQLite connection.
        num_ctx: New default context window in tokens.
    """
    set_setting(conn, _DEFAULT_NUM_CTX_KEY, str(clamp_num_ctx(num_ctx)))


_REMOTE_OLLAMA_ENABLED_KEY = "remote_ollama_enabled"


def get_remote_ollama_enabled(conn: sqlite3.Connection) -> bool:
    """Return whether the Remote Ollama agent is enabled app-wide.

    Default (no row): ``True``. Storing the key explicitly with value
    ``"0"`` disables the Remote agent everywhere — it's filtered out of
    the chat-header dropdown and existing chats with
    ``active_host="remote"`` degrade to Normal on their next turn.
    Defaulting to ``True`` preserves the post-phase-20a behavior on
    upgrade: if you've already set the env vars and used the agent, the
    DB row simply doesn't exist yet and everything keeps working.

    A malformed row (anything other than ``"1"`` / ``"0"``) is treated
    as the default — same forgiveness as the other typed accessors here.

    Args:
        conn: Open SQLite connection.
    """
    raw = get_setting(conn, _REMOTE_OLLAMA_ENABLED_KEY, default=None)
    if raw is None:
        return True
    return raw == "1"


def set_remote_ollama_enabled(
    conn: sqlite3.Connection, enabled: bool
) -> None:
    """Persist the app-wide Remote Ollama enable flag.

    Stored as ``"1"`` / ``"0"`` since the ``app_settings.value`` column
    is text.

    Args:
        conn: Open SQLite connection.
        enabled: True to keep the Remote agent visible/active, False to
            hide it from the dropdown and degrade in-flight chats.
    """
    set_setting(conn, _REMOTE_OLLAMA_ENABLED_KEY, "1" if enabled else "0")


def resolve_num_ctx_for_project(
    conn: sqlite3.Connection, project_num_ctx: int | None
) -> int:
    """Resolve the effective num_ctx for a turn: project override or global.

    Args:
        conn: Open SQLite connection.
        project_num_ctx: The project's ``num_ctx`` column value, or
            ``None`` when the project inherits the global default.

    Returns:
        A clamped, ready-to-use ``num_ctx`` token count for the Ollama
        request's ``options`` dict.
    """
    if project_num_ctx is not None:
        return clamp_num_ctx(project_num_ctx)
    return get_default_num_ctx(conn)
