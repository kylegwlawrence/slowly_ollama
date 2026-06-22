"""Registry of selectable Ollama *hosts* for the per-chat picker.

Originally a registry of user-invoked agents (Phase 16); repurposed into an
Ollama-host selector and renamed to ``host`` in Phase 23. The primary host
(``OLLAMA_HOST``) is the *absence* of a selection — the picker's leading
"host1" option, ``active_host`` NULL (see ``get_host``). Any number of
additional hosts are registered from ``config.extra_ollama_hosts()`` (the
``OLLAMA_EXTRA_HOSTS`` JSON list, with a legacy ``SLOWLY_OLLAMA_*`` single-host
fallback); selecting one routes a chat's inference to that machine, but
otherwise behaves like plain chat — the project prompt and the full tool
registry still apply (see ``app.routes._helpers._host_overrides``).
"""

import sqlite3
from dataclasses import dataclass
from urllib.parse import urlparse

from app.config import extra_ollama_hosts
from app.queries.settings import get_remote_ollama_enabled


@dataclass(frozen=True)
class HostSpec:
    """A selectable Ollama host.

    Attributes:
        name: Stable identifier persisted on the conversation
            (`conversations.active_host`) and used as the dropdown's option
            value. Lowercase snake_case.
        label: Human-readable name shown in the UI.
        description: One-line summary for the dropdown / tooltip.
        model: Default Ollama model id for this host, used as the per-chat
            fallback when the chat hasn't pinned a model for this machine.
        ollama_host: The host's Ollama base URL. Inference (chat probe, stream,
            compaction) targets this URL instead of the local ``OLLAMA_HOST``.
            Tools still execute on this server — only inference is offloaded.
    """

    name: str
    label: str
    description: str
    model: str
    ollama_host: str | None = None


# The primary host ("host1") is the *absence* of a selection (active_host
# NULL); the UI renders it as the leading picker option and it is NOT in this
# dict. Every registered entry is a non-primary host built from
# ``config.extra_ollama_hosts()`` (see ``_build_hosts``).
HOSTS: dict[str, HostSpec] = {}


def _build_hosts() -> dict[str, HostSpec]:
    """Build the host registry from ``config.extra_ollama_hosts()``.

    One ``HostSpec`` per configured non-primary host (the ``OLLAMA_EXTRA_HOSTS``
    JSON list, or the legacy ``SLOWLY_OLLAMA_*`` single-host fallback). Adding a
    machine to ``.env`` adds a picker option with no code change. A later
    duplicate ``name`` overwrites an earlier one (last wins) — defensive against
    a copy-paste in the config.

    Returns:
        A ``name -> HostSpec`` dict in declaration order.
    """
    hosts: dict[str, HostSpec] = {}
    for host in extra_ollama_hosts():
        hosts[host["name"]] = HostSpec(
            name=host["name"],
            label=host["label"],
            description=f"Run this chat on the '{host['label']}' Ollama host.",
            model=host["default_model"],
            ollama_host=host["url"],
        )
    return hosts


HOSTS = _build_hosts()


def list_hosts() -> list[HostSpec]:
    """Return all registered hosts in dropdown order."""
    return list(HOSTS.values())


def enabled_hosts(conn: sqlite3.Connection) -> list[HostSpec]:
    """Return hosts the user should currently see in the picker.

    Drops any host whose ``ollama_host`` is set when the app-wide Remote
    Ollama toggle is off (``app_settings.remote_ollama_enabled = "0"``).
    Local hosts (``ollama_host is None``) always pass through. With the
    toggle on this is the same set as :func:`list_hosts`.

    Routes use this for rendering the dropdown so a disabled remote host
    disappears from the UI without the registry having to be rebuilt.

    Args:
        conn: Open SQLite connection — the toggle lives in ``app_settings``.
    """
    if get_remote_ollama_enabled(conn):
        return list(HOSTS.values())
    return [h for h in HOSTS.values() if h.ollama_host is None]


def host_label(spec: HostSpec | None) -> str | None:
    """Human-readable hostname for a host's ``ollama_host``, or None.

    Used by the chat header chip so the user can see at a glance which
    machine a chat runs on (e.g. ``"host1"`` for ``http://host1:11434``).
    Local hosts (``ollama_host is None``) return ``None`` so the template can
    short-circuit without rendering the suffix.

    Falls back to the raw ``ollama_host`` value if ``urlparse`` can't
    extract a hostname — better to show *something* than swallow the
    label entirely.

    Args:
        spec: A ``HostSpec`` or ``None``. ``None`` returns ``None`` so the
            template can pass the active spec through without a guard.

    Returns:
        The hostname portion of the host's ``ollama_host``, the raw value when
        parsing fails, or ``None`` when the host runs on local Ollama (or
        ``spec`` is ``None``).
    """
    if spec is None or not spec.ollama_host:
        return None
    parsed = urlparse(spec.ollama_host)
    return parsed.hostname or spec.ollama_host


def get_host(name: str | None) -> HostSpec | None:
    """Resolve a host name to its spec.

    Args:
        name: The stored/submitted host name, or None/"" for the primary host.

    Returns:
        The matching `HostSpec`, or None for the primary host (empty/missing
        name) or an unknown name (defensive — e.g. a name persisted before a
        host was removed from the registry). A None result means "run on the
        primary host".
    """
    if not name:
        return None
    return HOSTS.get(name)


__all__ = [
    "HostSpec",
    "HOSTS",
    "enabled_hosts",
    "get_host",
    "host_label",
    "list_hosts",
]
