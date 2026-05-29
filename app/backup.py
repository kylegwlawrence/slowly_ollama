"""Phase 20: automatic, fire-and-forget backup/sync of state to a remote host.

The app is local-only, but the user runs it on more than one machine and
wants the chats database and agent workspaces to follow them. This module
*pushes* both to a remote host (e.g. host1) whenever local state changes —
when a message is sent, when generation completes, or when ``write_file``
succeeds.

Design (decisions locked with the user):

  * **Mirror semantics.** Each push overwrites a single canonical copy on
    the remote (``chats.db`` in one dir, the workspace tree in another),
    so the remote is always "the latest". It does NOT create a new
    timestamped folder per run — that would pile up thousands of folders
    when triggered per-message. A cheap server-side snapshot is taken at
    most once a day for safety (see ``_maybe_snapshot``).
  * **Single-flight + debounce.** ``request_backup`` is fire-and-forget and
    coalesces bursts: at most one push runs at a time, and a short quiet
    period batches the flurry of triggers around a single chat turn into
    one push.
  * **Offline-safe.** When the remote config is unset, or the host is
    unreachable, every path no-ops quietly. Backups must never error,
    block, or hang the chat path — they ride on the event loop as a
    background task.
  * **WAL-consistent.** The DB runs in WAL mode (``chats.db`` +
    ``-wal``/``-shm``). rsync-ing the live files can copy a torn state, so
    we first produce a consistent copy via SQLite's online backup API into
    a temp file and push *that* — never the live sidecar files.

Restore/pull is intentionally out of scope here (push only); use the pull-only
``app/copy_agent_workspace.py`` (e.g. ``--all``) to seed a fresh machine — see
``RESTORE.md``.
"""

import asyncio
import logging
import shlex
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from app import config
from app.config import db_path, file_tool_root
from app.connection import open_connection
from app.queries.settings import get_setting, set_setting

logger = logging.getLogger(__name__)

# Quiet period (seconds) the debounce loop waits before each push so a
# burst of triggers around one chat turn collapses into a single rsync.
# Module-level so tests can drop it to 0.
DEBOUNCE_SECONDS = 3.0

# How often the cheap server-side snapshot of the mirror is taken.
SNAPSHOT_INTERVAL = timedelta(days=1)

# app_settings key holding the ISO timestamp of the last snapshot.
_LAST_SNAPSHOT_KEY = "backup_last_snapshot_at"

# Same noise the manual copy script skips; keeps the mirror clean.
RSYNC_EXCLUDES = [
    "--exclude", ".DS_Store",
    "--exclude", "__pycache__",
    "--exclude", "*.pyc",
    "--exclude", ".pytest_cache",
    "--exclude", ".coverage",
]

# Non-interactive ssh: never hang on a host-key or password prompt; give
# up fast when the remote is asleep / off the network.
_SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
_RSYNC_SSH = "ssh -o BatchMode=yes -o ConnectTimeout=5"


# ---------------------------------------------------------------------------
# Scheduler: single-flight + debounce
# ---------------------------------------------------------------------------

# `_pending` is the dirty flag; `_task` is the lone in-flight debounce loop.
# A burst of `request_backup` calls flips `_pending` and (re)starts the loop
# only if one isn't already running — that's the single-flight guarantee.
_pending = False
_task: asyncio.Task | None = None


# ---------------------------------------------------------------------------
# Observable status (Phase 21: the UI chip)
# ---------------------------------------------------------------------------

# Process-local backup status, surfaced to the chat-header chip. Same pattern
# as `generation.live_generations` / `rag_health._cache`: one piece of
# cross-request in-memory state per concern, deliberately NOT on `app.state`.
# Reflects the single global backup task, so every chat sees the same value.
#
#   idle     no push has run yet this process (neutral resting state)
#   pending  a push is scheduled (debounce window) — drives the spinner
#   pushing  the rsync is in flight
#   ok       last push succeeded (green)
#   offline  no remote host was reachable — mirror asleep, NOT an error (grey)
#   failed   a reachable host's push errored (red)
BackupState = Literal["idle", "pending", "pushing", "ok", "offline", "failed"]

_status: BackupState = "idle"
_status_at: datetime | None = None


def backup_status() -> tuple[BackupState, datetime | None]:
    """Return the current ``(state, changed-at)`` for the UI chip.

    Process-local and synchronous (no I/O); reflects the single global backup
    task. ``idle`` means no push has run yet this process. Never raises.

    Returns:
        Tuple of the current :data:`BackupState` and the time it was last set
        (``None`` while still ``idle``).
    """
    return _status, _status_at


def _set_status(state: BackupState) -> None:
    """Record a status transition, timestamped with the wall clock."""
    global _status, _status_at
    _status = state
    _status_at = datetime.now()


def request_backup(reason: str) -> None:
    """Schedule a coalesced remote push. Fire-and-forget; safe to call often.

    No-ops immediately when backups aren't configured (see
    :func:`app.config.backups_enabled`). Otherwise it marks state dirty and
    ensures the debounce loop is running. Returns at once — callers never
    pay push latency on the request path.

    Must be called from within a running event loop (all call sites — the
    send endpoint, the generation done-callback, the tool loop — are).

    Args:
        reason: Short tag for logs (``"send"`` / ``"generation-complete"``
            / ``"write"``). Purely diagnostic.
    """
    global _pending, _task
    if not config.backups_enabled():
        return
    logger.debug("backup requested (%s)", reason)
    # Mark pending synchronously so the chip's first poll (≤2s out) is certain
    # to catch a spinner, even if the actual rsync later completes sub-second.
    _set_status("pending")
    _pending = True
    if _task is None or _task.done():
        _task = asyncio.create_task(_debounce_loop())


async def _debounce_loop() -> None:
    """Drain the dirty flag, sleeping a quiet period before each push.

    Clears ``_pending`` *before* sleeping so any trigger that arrives
    during the sleep or the push re-arms the flag and earns another pass.
    A push never raises out of here — a failed backup must not kill the
    loop (or, via an unhandled task exception, spam the logs).
    """
    global _pending
    while _pending:
        _pending = False
        await asyncio.sleep(DEBOUNCE_SECONDS)
        _set_status("pushing")
        try:
            await _run_backup_once()
        except Exception:  # noqa: BLE001 — backups never break the app
            logger.exception("backup push failed")


# ---------------------------------------------------------------------------
# One push
# ---------------------------------------------------------------------------


async def _run_backup_once() -> None:
    """Push the DB and workspaces to their mirrors, then maybe snapshot.

    Re-reads config each call (it may have changed since scheduling) and
    skips any half whose host is unreachable. The periodic snapshot only
    fires when at least one half actually pushed.
    """
    db_remote = config.remote_db_path()
    ws_remote = config.remote_workspace_path()
    if not db_remote or not ws_remote:
        # Config vanished since scheduling — settle the chip rather than
        # stranding it on "pushing".
        _set_status("offline")
        return

    db_target = _parse_remote(db_remote)
    ws_target = _parse_remote(ws_remote)
    if db_target is None or ws_target is None:
        logger.warning(
            "backup: REMOTE_DB_PATH / REMOTE_PATH must be host:/path specs"
        )
        _set_status("failed")
        return

    db_host, db_dir = db_target
    ws_host, ws_dir = ws_target

    # Cache reachability per host so two remotes on the same box probe once.
    reachable: dict[str, bool] = {}

    async def _ok(host: str) -> bool:
        if host not in reachable:
            reachable[host] = await _host_reachable(host)
            if not reachable[host]:
                logger.debug("backup: %s unreachable; skipping", host)
        return reachable[host]

    # Track each reachable half's outcome: None = host not reached (skipped).
    pushed = False
    db_ok: bool | None = None
    ws_ok: bool | None = None
    if await _ok(db_host):
        db_ok = await _push_database(db_host, db_dir)
        pushed = db_ok or pushed
    if await _ok(ws_host):
        ws_ok = await _push_workspaces(ws_host, ws_dir)
        pushed = ws_ok or pushed

    # Classify for the chip. No host reachable → offline (mirror asleep, not an
    # error). Any reachable half that errored → failed. Otherwise → ok.
    if not (reachable.get(db_host) or reachable.get(ws_host)):
        _set_status("offline")
    elif db_ok is False or ws_ok is False:
        _set_status("failed")
    else:
        _set_status("ok")

    if pushed:
        await _maybe_snapshot(db_host, db_dir, ws_host, ws_dir, reachable)


async def _push_database(host: str, remote_dir: str) -> bool:
    """Push a consistent copy of the DB to ``<host>:<remote_dir>/chats.db``.

    Produces the copy off the event loop via the SQLite backup API (so the
    live ``-wal``/``-shm`` are never shipped mid-write), then rsyncs the
    single file. Returns whether the push succeeded.
    """
    backup_file = await asyncio.to_thread(_write_consistent_db_copy)
    if backup_file is None:
        return False
    if await _run(["ssh", *_SSH_OPTS, host, f"mkdir -p {shlex.quote(remote_dir)}"]):
        return False
    rc = await _run([
        "rsync", "-az", "-e", _RSYNC_SSH, *RSYNC_EXCLUDES,
        str(backup_file), f"{host}:{remote_dir}/chats.db",
    ])
    return rc == 0


async def _push_workspaces(host: str, remote_dir: str) -> bool:
    """Mirror the workspace tree to ``<host>:<remote_dir>/``.

    No-ops when ``FILE_TOOL_ROOT`` is unset or missing on disk (nothing to
    back up). Additive sync — no ``--delete`` — so a momentarily-empty or
    misconfigured local root can never wipe the remote. Returns whether the
    push succeeded.
    """
    ws_root = file_tool_root()
    if ws_root is None or not ws_root.exists():
        return False
    if await _run(["ssh", *_SSH_OPTS, host, f"mkdir -p {shlex.quote(remote_dir)}"]):
        return False
    rc = await _run([
        "rsync", "-az", "-e", _RSYNC_SSH, *RSYNC_EXCLUDES,
        f"{ws_root}/", f"{host}:{remote_dir}/",
    ])
    return rc == 0


def _write_consistent_db_copy() -> Path | None:
    """Write a transactionally-consistent copy of the DB next to the original.

    Uses :meth:`sqlite3.Connection.backup` on a fresh read connection: in
    WAL mode the reader sees the last committed state, so the resulting
    file is internally consistent without touching the app's shared
    connection. Runs in a worker thread (it's blocking C).

    Returns:
        Path to ``chats.db.backup`` beside the live DB, or ``None`` when
        the live DB doesn't exist yet.
    """
    src_path = db_path()
    if not src_path.exists():
        return None
    dest = src_path.parent / "chats.db.backup"
    with closing(open_connection(src_path)) as src, \
            closing(sqlite3.connect(dest)) as dst:
        src.backup(dst)
    return dest


# ---------------------------------------------------------------------------
# Periodic server-side snapshot
# ---------------------------------------------------------------------------


async def _maybe_snapshot(
    db_host: str,
    db_dir: str,
    ws_host: str,
    ws_dir: str,
    reachable: dict[str, bool],
) -> None:
    """Once per ``SNAPSHOT_INTERVAL``, snapshot each mirror server-side.

    The snapshot is a ``cp -a`` on the remote into a sibling
    ``<dir>_snapshots/<timestamp>`` folder — no re-upload, cheap. Records
    the time only after both reachable halves are attempted, so a missed
    day simply retries on the next push rather than silently skipping.
    """
    if not _snapshot_due():
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if reachable.get(db_host):
        await _snapshot_remote_dir(db_host, db_dir, ts)
    if reachable.get(ws_host):
        await _snapshot_remote_dir(ws_host, ws_dir, ts)
    _record_snapshot()


async def _snapshot_remote_dir(host: str, remote_dir: str, ts: str) -> None:
    """Copy ``<remote_dir>`` into ``<remote_dir>_snapshots/<ts>`` on host."""
    base = remote_dir.rstrip("/")
    snap_base = f"{base}_snapshots"
    cmd = (
        f"mkdir -p {shlex.quote(snap_base)} && "
        f"cp -a {shlex.quote(base)} {shlex.quote(snap_base + '/' + ts)}"
    )
    await _run(["ssh", *_SSH_OPTS, host, cmd])


def _snapshot_due() -> bool:
    """Return whether a snapshot is overdue (or has never been taken)."""
    with closing(open_connection()) as conn:
        raw = get_setting(conn, _LAST_SNAPSHOT_KEY)
    if raw is None:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    return datetime.now() - last >= SNAPSHOT_INTERVAL


def _record_snapshot() -> None:
    """Persist 'now' as the last-snapshot time."""
    with closing(open_connection()) as conn:
        set_setting(conn, _LAST_SNAPSHOT_KEY, datetime.now().isoformat())


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _parse_remote(spec: str) -> tuple[str, str] | None:
    """Split a ``host:/path`` spec into ``(host, path)``, or ``None``.

    Mirrors ``copy_agent_workspace.parse_remote``: a spec is "remote" only
    when it contains a colon.
    """
    if ":" not in spec:
        return None
    host, path = spec.split(":", 1)
    if not host or not path:
        return None
    return host, path


async def _host_reachable(host: str) -> bool:
    """Return whether ``ssh <host> true`` succeeds within the connect timeout."""
    return await _run(["ssh", *_SSH_OPTS, host, "true"]) == 0


async def _run(cmd: list[str]) -> int:
    """Run a command off the event loop; return its exit code.

    Captures output so a failure logs the stderr at debug level instead of
    leaking to the console. Never raises for a non-zero exit — callers
    branch on the returned code.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        logger.debug(
            "backup command exited %s: %s",
            proc.returncode,
            err.decode(errors="replace").strip(),
        )
    return proc.returncode


# ---------------------------------------------------------------------------
# Manual restore (Phase 22: the "Pull" button)
# ---------------------------------------------------------------------------

# Hard ceiling on a manual pull so a hung ssh/rsync can't wedge the request
# that triggered it. The pull script's own connectivity probe gives up after
# ~5s, so a sleeping mirror fails fast; this only catches a genuine stall.
PULL_TIMEOUT = 180.0


async def pull_all(timeout: float = PULL_TIMEOUT) -> tuple[bool, str | None]:
    """Restore the DB + workspaces from the remote mirror (the ``--all`` pull).

    Shells out to ``python copy_agent_workspace.py --all`` — pull-only and
    ``.env``-driven, the same command a user runs by hand to seed a fresh
    machine. Running it as a child process isolates that script's
    ``sys.exit()`` error paths from the app.

    The CALLER must ensure no live SQLite connection holds the local
    ``chats.db`` open: the script overwrites it (see the route in
    ``app/routes/chats.py``, which closes ``app.state.db`` around this call).
    Never raises — failures come back as ``(False, detail)``.

    Args:
        timeout: Seconds to wait before killing a stalled pull.

    Returns:
        ``(ok, detail)``. ``ok`` is True on a clean restore. On failure
        ``detail`` is a short human message (``"Mirror unreachable"`` or the
        last line of the script's output) for the chip's error label.
    """
    script = Path(__file__).resolve().parent / "copy_agent_workspace.py"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        "--all",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(script.parent.parent),
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        logger.warning("backup pull timed out after %ss", timeout)
        return False, "Mirror unreachable"

    if proc.returncode == 0:
        return True, None

    text = out.decode(errors="replace") if out else ""
    logger.warning("backup pull failed (exit %s): %s", proc.returncode, text.strip())
    if "Cannot connect" in text:
        # The script's SSH probe failed — the mirror is asleep/off-network.
        # Match only this explicit message: the script also prints "Connection
        # successful" on a REACHABLE host, so a looser check would misread a
        # reachable-but-rsync-failed pull as unreachable.
        return False, "Mirror unreachable"
    last = text.strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
    return False, last[0][:80]
