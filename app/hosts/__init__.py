"""Registry of selectable Ollama *hosts* for the per-chat picker.

Every host a chat can run on is a :class:`HostSpec`, including the **primary**
host (the local ``OLLAMA_HOST``). The primary host is built on demand by
:func:`get_primary_host` rather than frozen into :data:`HOSTS`, because its
label (the ``OLLAMA_HOST`` hostname) and effective model (the chat's pinned
model, else the global default) are resolved at request time. Additional hosts
come from ``config.extra_ollama_hosts()`` and ARE frozen into :data:`HOSTS` at
import (their ``default_model`` is static).

Resolution always yields a concrete ``HostSpec``: :func:`get_host` returns the
primary host for an empty/missing name and raises :class:`UnknownHostError` for
a name that isn't registered — never ``None``. Stale names don't reach
resolution: the app reconciles ``conversations.active_host`` against this
registry at startup (``app.queries.conversations.clear_unknown_active_hosts``),
so an unknown name signals a bug or stale client input, not an expected state.
In the database the primary host is still encoded as ``active_host`` NULL;
``get_host(None)`` maps that NULL to the primary spec.
"""

import sqlite3
from dataclasses import dataclass
from urllib.parse import urlparse

from app import config
from app.queries.settings import get_remote_ollama_enabled

# Stable name of the primary host (local ``OLLAMA_HOST``). It is NOT what gets
# stored: a chat on the primary host has ``active_host`` NULL. The name only
# labels the resolved spec and distinguishes it via :attr:`HostSpec.is_primary`.
PRIMARY_HOST_NAME = "primary"


class UnknownHostError(ValueError):
    """Raised when a name isn't the primary host and isn't a registered host.

    Resolution treats this as a bug: startup reconciliation clears stale names
    from the DB, so the only callers that can hit it are HTTP boundaries fed
    raw client input (a stale dropdown post), which catch it and fall back to
    the primary host.
    """


@dataclass(frozen=True)
class HostSpec:
    """A selectable Ollama host.

    Attributes:
        name: Stable identifier. For extra hosts it's the config ``name`` and
            the dropdown option value; the primary host uses
            ``PRIMARY_HOST_NAME``. Lowercase snake_case.
        label: Human-readable name shown in the UI.
        description: One-line summary for the dropdown / tooltip.
        model: Default model id. Authoritative for extra hosts; empty for the
            primary host, whose effective model is the chat's pinned model
            (resolved downstream, not read from here).
        ollama_host: Base URL inference targets instead of the local
            ``OLLAMA_HOST``. ``None`` marks the primary (local) host. Only
            inference is offloaded — tool calls still run in this app process.
    """

    name: str
    label: str
    description: str
    model: str
    ollama_host: str | None = None

    @property
    def is_primary(self) -> bool:
        """Whether this is the primary (local ``OLLAMA_HOST``) host."""
        return self.name == PRIMARY_HOST_NAME


def _primary_host_label() -> str:
    """Return the primary host's label: the ``OLLAMA_HOST`` hostname.

    Falls back to the raw URL when ``urlparse`` finds no hostname, or to
    ``"default"`` when ``OLLAMA_HOST`` is unset — render a neutral label rather
    than 500 a page over a misconfiguration.
    """
    try:
        raw = config.ollama_host()
    except KeyError:
        return "default"
    return urlparse(raw).hostname or raw


def get_primary_host() -> HostSpec:
    """Build the primary host spec from config, fresh on each call.

    Per-call (not a module-level constant) so the label tracks the current
    ``OLLAMA_HOST`` — env that ``.env`` edits and tests monkeypatch at runtime.
    """
    return HostSpec(
        name=PRIMARY_HOST_NAME,
        label=_primary_host_label(),
        description="Run this chat on the local Ollama host.",
        model="",
        ollama_host=None,
    )


# Extra (non-primary) hosts only, frozen at import. The primary host is NOT
# stored here — it's built on demand by ``get_primary_host`` (its label/model
# are dynamic); ``list_hosts`` / ``enabled_hosts`` prepend it.
HOSTS: dict[str, HostSpec] = {}


def _build_hosts() -> dict[str, HostSpec]:
    """Build the extra-host registry from ``config.extra_ollama_hosts()``.

    One ``HostSpec`` per configured non-primary host, so adding a machine to
    ``.env`` adds a picker option with no code change. Duplicate names are
    last-wins.

    Returns:
        A ``name -> HostSpec`` dict in declaration order.
    """
    hosts: dict[str, HostSpec] = {}
    for host in config.extra_ollama_hosts():
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
    """Return every host in dropdown order: the primary host, then the extras."""
    return [get_primary_host(), *HOSTS.values()]


def enabled_hosts(conn: sqlite3.Connection) -> list[HostSpec]:
    """Return hosts visible in the picker, in dropdown order (primary first).

    The primary (local) host is always shown. When the app-wide Remote Ollama
    toggle is off, extra hosts with an ``ollama_host`` are dropped; with it on,
    this matches :func:`list_hosts`. Lets the dropdown hide a disabled remote
    host without rebuilding the registry.

    Args:
        conn: Open SQLite connection — the toggle lives in ``app_settings``.
    """
    primary = get_primary_host()
    if get_remote_ollama_enabled(conn):
        return [primary, *HOSTS.values()]
    return [primary, *(h for h in HOSTS.values() if h.ollama_host is None)]


def get_host(name: str | None) -> HostSpec:
    """Resolve a host name to its spec; never returns ``None``.

    Args:
        name: A stored/submitted host name. Empty/missing or
            ``PRIMARY_HOST_NAME`` resolves to the primary host.

    Returns:
        The matching ``HostSpec``.

    Raises:
        UnknownHostError: When ``name`` is non-empty, isn't the primary host,
            and isn't a registered extra host. Startup reconciliation prevents
            this from a stored value, so it signals a bug or stale client input.
    """
    if not name or name == PRIMARY_HOST_NAME:
        return get_primary_host()
    try:
        return HOSTS[name]
    except KeyError as e:
        raise UnknownHostError(name) from e


__all__ = [
    "HostSpec",
    "HOSTS",
    "PRIMARY_HOST_NAME",
    "UnknownHostError",
    "enabled_hosts",
    "get_host",
    "get_primary_host",
    "list_hosts",
]
