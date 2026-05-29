"""Tests for app.backup: the fire-and-forget remote backup scheduler.

Hermetic and mock-only — no real ssh/rsync, no network, no host1. The
subprocess seam (``_run`` / ``_host_reachable``) and the DB-copy seam
(``_write_consistent_db_copy``) are patched so we exercise the scheduler
logic, the offline guard, the rsync targeting, and the snapshot gating
without touching the outside world.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app import backup, config
from app.db import initialize_database


@pytest.fixture(autouse=True)
def _reset_backup_state(monkeypatch: pytest.MonkeyPatch):
    """Isolate the module-level scheduler globals around every test.

    ``_pending`` / ``_task`` persist across calls by design (single-flight),
    so without this a leftover task or dirty flag would leak between tests.
    Also collapse the debounce wait to zero so tests don't sleep.
    """
    backup._pending = False
    backup._task = None
    backup._status = "idle"
    backup._status_at = None
    monkeypatch.setattr(backup, "DEBOUNCE_SECONDS", 0)
    yield
    backup._pending = False
    backup._task = None
    backup._status = "idle"
    backup._status_at = None


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure both remote destinations so backups are enabled."""
    monkeypatch.setenv("REMOTE_PATH", "host1:/remote/agent_workspaces")
    monkeypatch.setenv("REMOTE_DB_PATH", "host1:/remote/olliellama_chats")


# ---------------------------------------------------------------------------
# Config gating
# ---------------------------------------------------------------------------


def test_request_backup_noops_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With either remote unset, request_backup schedules nothing."""
    monkeypatch.setenv("REMOTE_PATH", "host1:/remote/agent_workspaces")
    monkeypatch.delenv("REMOTE_DB_PATH", raising=False)
    assert config.backups_enabled() is False

    backup.request_backup("send")

    assert backup._task is None
    assert backup._pending is False


def test_backups_enabled_requires_both_remotes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """backups_enabled() is True only when both destinations are set."""
    monkeypatch.delenv("REMOTE_PATH", raising=False)
    monkeypatch.delenv("REMOTE_DB_PATH", raising=False)
    assert config.backups_enabled() is False

    monkeypatch.setenv("REMOTE_DB_PATH", "host1:/db")
    assert config.backups_enabled() is False

    monkeypatch.setenv("REMOTE_PATH", "host1:/ws")
    assert config.backups_enabled() is True


# ---------------------------------------------------------------------------
# Scheduler: debounce + single-flight
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_backup_runs_once_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single trigger drives exactly one push."""
    _enable(monkeypatch)
    runs = AsyncMock()
    monkeypatch.setattr(backup, "_run_backup_once", runs)

    backup.request_backup("send")
    await backup._task

    runs.assert_awaited_once()


@pytest.mark.asyncio
async def test_debounce_coalesces_a_burst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synchronous burst of triggers collapses into one push.

    All five calls land before the scheduled loop task gets to run, so the
    dirty flag is consumed once.
    """
    _enable(monkeypatch)
    runs = AsyncMock()
    monkeypatch.setattr(backup, "_run_backup_once", runs)

    for _ in range(5):
        backup.request_backup("send")
    await backup._task

    runs.assert_awaited_once()


@pytest.mark.asyncio
async def test_single_flight_schedules_one_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trigger arriving mid-push earns exactly one more push, not a pile."""
    _enable(monkeypatch)
    calls = {"n": 0}

    async def _fake_run() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            # Arrives while the first push is "in flight".
            backup.request_backup("write")

    monkeypatch.setattr(backup, "_run_backup_once", _fake_run)

    backup.request_backup("send")
    await backup._task

    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_push_failure_does_not_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception in the push is swallowed — the loop never raises."""
    _enable(monkeypatch)
    monkeypatch.setattr(
        backup, "_run_backup_once", AsyncMock(side_effect=RuntimeError("boom"))
    )

    backup.request_backup("send")
    await backup._task  # must not raise


# ---------------------------------------------------------------------------
# One push: offline guard + rsync targeting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_backup_once_skips_when_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the host can't be reached, no push or snapshot is attempted."""
    _enable(monkeypatch)
    monkeypatch.setattr(backup, "_host_reachable", AsyncMock(return_value=False))
    db_push = AsyncMock(return_value=True)
    ws_push = AsyncMock(return_value=True)
    snap = AsyncMock()
    monkeypatch.setattr(backup, "_push_database", db_push)
    monkeypatch.setattr(backup, "_push_workspaces", ws_push)
    monkeypatch.setattr(backup, "_maybe_snapshot", snap)

    await backup._run_backup_once()

    db_push.assert_not_awaited()
    ws_push.assert_not_awaited()
    snap.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_backup_once_skips_when_remote_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If config is cleared between schedule and run, the push bails."""
    monkeypatch.delenv("REMOTE_DB_PATH", raising=False)
    monkeypatch.setenv("REMOTE_PATH", "host1:/ws")
    reach = AsyncMock(return_value=True)
    monkeypatch.setattr(backup, "_host_reachable", reach)

    await backup._run_backup_once()

    reach.assert_not_awaited()


@pytest.mark.asyncio
async def test_push_database_targets_chats_db_and_never_wal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DB push rsyncs the consistent copy to <dir>/chats.db only.

    The live -wal/-shm sidecars must never appear in the rsync args — that's
    the whole point of going through the backup API.
    """
    monkeypatch.setattr(
        backup,
        "_write_consistent_db_copy",
        lambda: Path("/local/data/chats.db.backup"),
    )
    commands: list[list[str]] = []

    async def _capture(cmd: list[str]) -> int:
        commands.append(cmd)
        return 0

    monkeypatch.setattr(backup, "_run", _capture)

    ok = await backup._push_database("host1", "/remote/olliellama_chats")

    assert ok is True
    rsync = next(c for c in commands if c[0] == "rsync")
    assert rsync[-1] == "host1:/remote/olliellama_chats/chats.db"
    assert rsync[-2] == "/local/data/chats.db.backup"
    joined = " ".join(rsync)
    assert "-wal" not in joined and "-shm" not in joined


@pytest.mark.asyncio
async def test_push_database_noops_without_db_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No live DB → nothing to copy, no commands run."""
    monkeypatch.setattr(backup, "_write_consistent_db_copy", lambda: None)
    run = AsyncMock(return_value=0)
    monkeypatch.setattr(backup, "_run", run)

    ok = await backup._push_database("host1", "/remote/db")

    assert ok is False
    run.assert_not_awaited()


# ---------------------------------------------------------------------------
# Periodic server-side snapshot
# ---------------------------------------------------------------------------


@pytest.fixture
def _temp_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Point DB_PATH at a fresh initialized SQLite file for settings reads."""
    db_file = tmp_path / "chats.db"
    monkeypatch.setenv("DB_PATH", str(db_file))
    initialize_database(db_file)
    return db_file


def test_snapshot_due_when_never_taken(_temp_db: Path) -> None:
    """With no recorded snapshot, one is due."""
    assert backup._snapshot_due() is True


def test_snapshot_not_due_when_recent(_temp_db: Path) -> None:
    """A snapshot taken just now is not yet due again."""
    backup._record_snapshot()
    assert backup._snapshot_due() is False


def test_snapshot_due_when_stale(_temp_db: Path) -> None:
    """An old recorded snapshot is due again."""
    from app.connection import open_connection
    from app.queries.settings import set_setting

    stale = (datetime.now() - timedelta(days=2)).isoformat()
    with open_connection(_temp_db) as conn:
        set_setting(conn, backup._LAST_SNAPSHOT_KEY, stale)

    assert backup._snapshot_due() is True


@pytest.mark.asyncio
async def test_maybe_snapshot_runs_cp_and_records_when_due(
    monkeypatch: pytest.MonkeyPatch, _temp_db: Path
) -> None:
    """When due, both reachable mirrors get a server-side cp and the time is saved."""
    commands: list[list[str]] = []

    async def _capture(cmd: list[str]) -> int:
        commands.append(cmd)
        return 0

    monkeypatch.setattr(backup, "_run", _capture)

    await backup._maybe_snapshot(
        "host1", "/remote/db", "host1", "/remote/ws",
        {"host1": True},
    )

    # Two cp invocations (DB dir + workspace dir), each into *_snapshots/<ts>.
    cp_cmds = [c for c in commands if "cp -a" in c[-1]]
    assert len(cp_cmds) == 2
    assert any("/remote/db_snapshots/" in c[-1] for c in cp_cmds)
    assert any("/remote/ws_snapshots/" in c[-1] for c in cp_cmds)
    assert backup._snapshot_due() is False  # recorded


@pytest.mark.asyncio
async def test_maybe_snapshot_skips_when_not_due(
    monkeypatch: pytest.MonkeyPatch, _temp_db: Path
) -> None:
    """A recent snapshot means no server-side cp this push."""
    backup._record_snapshot()
    run = AsyncMock(return_value=0)
    monkeypatch.setattr(backup, "_run", run)

    await backup._maybe_snapshot(
        "host1", "/remote/db", "host1", "/remote/ws", {"host1": True}
    )

    run.assert_not_awaited()


# ---------------------------------------------------------------------------
# Helpers + the write-trigger contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("remote-host:/srv/db", ("remote-host", "/srv/db")),
        ("/local/only", None),
        ("host:", None),
        (":/path", None),
    ],
)
def test_parse_remote(spec: str, expected) -> None:
    """Only host:/path specs parse; bare local paths and partials don't."""
    assert backup._parse_remote(spec) == expected


@pytest.mark.asyncio
async def test_run_executes_local_command() -> None:
    """_run returns the real exit code without raising on non-zero."""
    assert await backup._run(["true"]) == 0
    assert await backup._run(["false"]) != 0


@pytest.mark.asyncio
async def test_run_backup_once_pushes_both_then_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: both halves reachable → both push, then snapshot fires."""
    _enable(monkeypatch)
    monkeypatch.setattr(backup, "_host_reachable", AsyncMock(return_value=True))
    db_push = AsyncMock(return_value=True)
    ws_push = AsyncMock(return_value=False)
    snap = AsyncMock()
    monkeypatch.setattr(backup, "_push_database", db_push)
    monkeypatch.setattr(backup, "_push_workspaces", ws_push)
    monkeypatch.setattr(backup, "_maybe_snapshot", snap)

    await backup._run_backup_once()

    db_push.assert_awaited_once()
    ws_push.assert_awaited_once()
    snap.assert_awaited_once()  # at least one half pushed → snapshot considered


@pytest.mark.asyncio
async def test_run_backup_once_warns_on_bad_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-host:/path spec aborts the push (no reachability probe)."""
    monkeypatch.setenv("REMOTE_PATH", "no-colon-here")
    monkeypatch.setenv("REMOTE_DB_PATH", "host1:/db")
    reach = AsyncMock(return_value=True)
    monkeypatch.setattr(backup, "_host_reachable", reach)

    await backup._run_backup_once()

    reach.assert_not_awaited()


@pytest.mark.asyncio
async def test_push_database_bails_when_mkdir_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing remote mkdir stops the push before rsync."""
    monkeypatch.setattr(
        backup, "_write_consistent_db_copy", lambda: Path("/local/chats.db.backup")
    )
    monkeypatch.setattr(backup, "_run", AsyncMock(return_value=1))

    assert await backup._push_database("host1", "/remote/db") is False


@pytest.mark.asyncio
async def test_push_workspaces_syncs_with_trailing_slash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Workspace push mkdir's the remote dir then rsyncs the tree."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    (tmp_path / "f.txt").write_text("x")
    commands: list[list[str]] = []

    async def _capture(cmd: list[str]) -> int:
        commands.append(cmd)
        return 0

    monkeypatch.setattr(backup, "_run", _capture)

    ok = await backup._push_workspaces("host1", "/remote/ws")

    assert ok is True
    rsync = next(c for c in commands if c[0] == "rsync")
    assert rsync[-1] == "host1:/remote/ws/"
    assert rsync[-2] == f"{tmp_path}/"


@pytest.mark.asyncio
async def test_push_workspaces_noops_without_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No FILE_TOOL_ROOT → nothing to back up."""
    monkeypatch.delenv("FILE_TOOL_ROOT", raising=False)
    run = AsyncMock(return_value=0)
    monkeypatch.setattr(backup, "_run", run)

    assert await backup._push_workspaces("host1", "/remote/ws") is False
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_push_workspaces_bails_when_mkdir_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failing remote mkdir stops the workspace push before rsync."""
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    monkeypatch.setattr(backup, "_run", AsyncMock(return_value=1))

    assert await backup._push_workspaces("host1", "/remote/ws") is False


@pytest.mark.asyncio
async def test_host_reachable_reflects_run_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_host_reachable is True iff the probe command exits zero."""
    monkeypatch.setattr(backup, "_run", AsyncMock(return_value=0))
    assert await backup._host_reachable("host1") is True
    monkeypatch.setattr(backup, "_run", AsyncMock(return_value=255))
    assert await backup._host_reachable("host1") is False


def test_snapshot_due_on_malformed_timestamp(_temp_db: Path) -> None:
    """A corrupt last-snapshot value is treated as due rather than raising."""
    conn = sqlite3.connect(_temp_db)
    try:
        from app.queries.settings import set_setting

        set_setting(conn, backup._LAST_SNAPSHOT_KEY, "not-a-timestamp")
    finally:
        conn.close()

    assert backup._snapshot_due() is True


def test_write_consistent_db_copy_produces_a_readable_file(
    _temp_db: Path,
) -> None:
    """The backup-API copy lands beside the live DB and is itself a DB."""
    dest = backup._write_consistent_db_copy()

    assert dest == _temp_db.parent / "chats.db.backup"
    assert dest.exists()
    # The copy opens as a valid SQLite DB carrying the schema.
    conn = sqlite3.connect(dest)
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            )
        }
    finally:
        conn.close()
    assert "app_settings" in names


def test_write_consistent_db_copy_returns_none_without_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No live DB file → no copy."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "missing.db"))
    assert backup._write_consistent_db_copy() is None


def test_write_trigger_predicate_matches_real_write_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The generation hook's success check matches write_file's real output.

    The trigger fires on ``result.text.startswith("Wrote ")``; verify a real
    successful write produces that prefix while a rejected path does not.
    """
    monkeypatch.setenv("FILE_TOOL_ROOT", str(tmp_path))
    from app.tools.builtins import write_file

    ok = write_file("notes.txt", "hello")
    assert ok.startswith("Wrote ")

    rejected = write_file("../escape.txt", "nope")
    assert not rejected.startswith("Wrote ")


# ---------------------------------------------------------------------------
# Phase 21: observable status for the UI chip
# ---------------------------------------------------------------------------


def test_backup_status_starts_idle() -> None:
    """A fresh process reports idle with no timestamp."""
    state, at = backup.backup_status()
    assert state == "idle"
    assert at is None


@pytest.mark.asyncio
async def test_request_backup_sets_pending_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """request_backup flips status to pending synchronously (drives the spinner).

    Asserted before the debounce loop is awaited: the dirty flag and pending
    status are set on the calling stack, ahead of the push that overwrites them.
    """
    _enable(monkeypatch)
    monkeypatch.setattr(backup, "_run_backup_once", AsyncMock())

    backup.request_backup("send")

    state, at = backup.backup_status()
    assert state == "pending"
    assert at is not None

    # Drain the spawned loop so it doesn't leak into the next test.
    if backup._task is not None:
        await backup._task


def test_request_backup_leaves_status_idle_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The disabled guard returns before touching status."""
    monkeypatch.delenv("REMOTE_DB_PATH", raising=False)
    monkeypatch.setenv("REMOTE_PATH", "host1:/ws")

    backup.request_backup("send")

    assert backup.backup_status()[0] == "idle"


@pytest.mark.asyncio
async def test_run_backup_once_status_ok_when_both_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both halves reachable and pushing → status ok (green)."""
    _enable(monkeypatch)
    monkeypatch.setattr(backup, "_host_reachable", AsyncMock(return_value=True))
    monkeypatch.setattr(backup, "_push_database", AsyncMock(return_value=True))
    monkeypatch.setattr(backup, "_push_workspaces", AsyncMock(return_value=True))
    monkeypatch.setattr(backup, "_maybe_snapshot", AsyncMock())

    await backup._run_backup_once()

    assert backup.backup_status()[0] == "ok"


@pytest.mark.asyncio
async def test_run_backup_once_status_offline_when_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No host reachable → status offline (grey), not failed."""
    _enable(monkeypatch)
    monkeypatch.setattr(backup, "_host_reachable", AsyncMock(return_value=False))
    monkeypatch.setattr(backup, "_push_database", AsyncMock(return_value=True))
    monkeypatch.setattr(backup, "_push_workspaces", AsyncMock(return_value=True))
    monkeypatch.setattr(backup, "_maybe_snapshot", AsyncMock())

    await backup._run_backup_once()

    assert backup.backup_status()[0] == "offline"


@pytest.mark.asyncio
async def test_run_backup_once_status_failed_when_reachable_push_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reachable but a half's push returns False → status failed (red)."""
    _enable(monkeypatch)
    monkeypatch.setattr(backup, "_host_reachable", AsyncMock(return_value=True))
    monkeypatch.setattr(backup, "_push_database", AsyncMock(return_value=True))
    monkeypatch.setattr(backup, "_push_workspaces", AsyncMock(return_value=False))
    monkeypatch.setattr(backup, "_maybe_snapshot", AsyncMock())

    await backup._run_backup_once()

    assert backup.backup_status()[0] == "failed"


@pytest.mark.asyncio
async def test_run_backup_once_status_offline_when_remote_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config cleared between schedule and run → settle to offline, not stranded."""
    monkeypatch.delenv("REMOTE_DB_PATH", raising=False)
    monkeypatch.setenv("REMOTE_PATH", "host1:/ws")
    backup._status = "pushing"

    await backup._run_backup_once()

    assert backup.backup_status()[0] == "offline"


@pytest.mark.asyncio
async def test_run_backup_once_status_failed_on_bad_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-host:/path spec → status failed (a real misconfiguration)."""
    monkeypatch.setenv("REMOTE_PATH", "no-colon-here")
    monkeypatch.setenv("REMOTE_DB_PATH", "host1:/db")

    await backup._run_backup_once()

    assert backup.backup_status()[0] == "failed"


@pytest.mark.asyncio
async def test_debounce_loop_sets_pushing_before_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop flips status to pushing right before the push runs."""
    _enable(monkeypatch)
    seen: list[str] = []

    async def _spy() -> None:
        seen.append(backup.backup_status()[0])

    monkeypatch.setattr(backup, "_run_backup_once", _spy)

    backup.request_backup("send")
    assert backup._task is not None
    await backup._task

    assert seen == ["pushing"]
