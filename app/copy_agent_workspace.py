#!/usr/bin/env python3
"""Restore the agent workspaces and/or the chats DB from a remote mirror.

**Pull-only by design.** Pushing is the running app's job: ``app/backup.py``
mirrors both DB and workspaces on every chat turn, shipping a
transactionally-consistent DB copy via the SQLite backup API. A manual push
could ship a torn WAL state or recreate stray timestamped folders, so this
tool never writes to the remote — it only reads the flat always-latest mirror
back down.

Modes:

  * **Workspaces (default).** Pull REMOTE_PATH → FILE_TOOL_ROOT.
  * **Database (`--db`).** Pull ``chats.db`` REMOTE_DB_PATH → DB_PATH (and
    clear stale local ``-wal``/``-shm`` sidecars from the old DB).
  * **Everything (`--all`).** Pull both — the seed-a-new-machine case.

Add ``--snapshot`` to restore from the latest dated snapshot under
``<remote_dir>_snapshots/<timestamp>/`` instead of the live mirror.

Defaults come from .env: REMOTE_PATH / FILE_TOOL_ROOT (workspaces) and
REMOTE_DB_PATH / DB_PATH (database). Override endpoints with ``--source``
(remote) and ``--dest`` (local); ``--all`` always uses .env.

Usage:
    python app/copy_agent_workspace.py                  # restore workspaces
    python app/copy_agent_workspace.py --snapshot       # latest workspace snapshot
    python app/copy_agent_workspace.py --db [--snapshot]
    python app/copy_agent_workspace.py --all [--snapshot]

    Remote format: host:/path/to/dir   Local format: path/to/dir
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

RSYNC_EXCLUDES = [
    "--exclude", ".DS_Store",
    "--exclude", "__pycache__",
    "--exclude", "*.pyc",
    "--exclude", ".pytest_cache",
    "--exclude", ".coverage",
]


def run_command(cmd: list[str], description: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command, exit with a message on failure."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=check)
        return result
    except subprocess.CalledProcessError as e:
        print(f"ERROR: {description} failed")
        print(f"Command: {' '.join(cmd)}")
        print(f"Error: {e.stderr}")
        sys.exit(1)


def parse_remote(arg: str) -> tuple[str, str] | None:
    """Return (host, path) if arg is a remote spec (host:/path), else None."""
    if ":" in arg:
        host, path = arg.split(":", 1)
        return host, path
    return None


def test_ssh_connection(host: str) -> bool:
    """Test SSH connectivity to host."""
    print("Testing SSH connection...")
    cmd = ["ssh", "-o", "ConnectTimeout=5", host, "echo 'Connection successful'"]
    result = run_command(cmd, "SSH connection test", check=False)

    if result.returncode != 0:
        print(f"ERROR: Cannot connect to {host}")
        print("Please check:")
        print("  1. Host is powered on and reachable")
        print("  2. SSH is enabled")
        print("  3. SSH config has correct hostname/username")
        return False

    print(result.stdout.strip())
    return True


def find_latest_remote_folder(host: str, remote_base: str) -> str:
    """Return the full path of the newest YYYYMMDD_HHMMSS folder on host.

    Used only by ``--snapshot``: ``app/backup.py`` drops dated snapshots into
    ``<dir>_snapshots/<timestamp>/``.
    """
    print("Finding latest snapshot on remote...")
    cmd = ["ssh", host, f"ls -t {remote_base} | grep -E '^[0-9]{{8}}_[0-9]{{6}}$' | head -1"]
    result = run_command(cmd, "List remote folders")

    latest = result.stdout.strip()
    if not latest:
        print(f"ERROR: No timestamped folders found under {remote_base} on {host}")
        sys.exit(1)

    return f"{remote_base}/{latest}"


def pull_database(host: str, remote_dir: str, local_dest: str, snapshot: bool = False) -> None:
    """Rsync the mirrored ``chats.db`` from the remote into ``local_dest``.

    The remote file is the consistent copy the app pushed via the SQLite
    backup API — a safe standalone database with no ``-wal``/``-shm`` sidecars.

    Args:
        host: Remote hostname.
        remote_dir: Remote dir holding ``chats.db`` (REMOTE_DB_PATH).
        local_dest: Local path to write ``chats.db`` to (DB_PATH).
        snapshot: When True, pull from the latest dated snapshot under
            ``<remote_dir>_snapshots/<timestamp>/`` instead of the live mirror.
    """
    dest_path = Path(local_dest).expanduser()
    base = remote_dir.rstrip("/")

    print("=" * 50)
    print("Restoring database from remote mirror")
    print("=" * 50)

    if not test_ssh_connection(host):
        sys.exit(1)

    if snapshot:
        latest = find_latest_remote_folder(host, f"{base}_snapshots")
        remote_db = f"{latest}/chats.db"
    else:
        remote_db = f"{base}/chats.db"

    print(f"Remote : {host}:{remote_db}")
    print(f"Local  : {dest_path}")
    print("WARNING: stop the app (uvicorn) before restoring — overwriting a")
    print("         live database can corrupt it.")
    print("=" * 50)

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    print("Copying database...")
    result = subprocess.run(
        ["rsync", "-avz", "--progress", *RSYNC_EXCLUDES, f"{host}:{remote_db}", str(dest_path)],
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: rsync failed with exit code {result.returncode}")
        sys.exit(1)

    # The pulled file is a standalone consistent copy. Leftover sidecars belong
    # to the OLD local DB; pairing them with the fresh file would corrupt it.
    for suffix in ("-wal", "-shm"):
        sidecar = dest_path.with_name(dest_path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
            print(f"Removed stale sidecar: {sidecar}")

    print("=" * 50)
    print("✓ Restore complete!")
    print("=" * 50)
    print(f"Database is now at: {dest_path}")
    print("Restart the app: uvicorn main:app --reload")


def pull_workspaces(host: str, remote_dir: str, local_dest: str, snapshot: bool = False) -> None:
    """Rsync the workspace tree from the remote mirror into ``local_dest``.

    Pulls the flat always-latest mirror that ``app/backup.py`` maintains. The
    sync is additive (no ``--delete``), so local-only files survive.

    Args:
        host: Remote hostname.
        remote_dir: Remote workspace dir (REMOTE_PATH).
        local_dest: Local workspace root (FILE_TOOL_ROOT).
        snapshot: When True, pull from the latest dated snapshot under
            ``<remote_dir>_snapshots/<timestamp>/`` instead of the live mirror.
    """
    dest_path = Path(local_dest).expanduser()
    base = remote_dir.rstrip("/")

    print("=" * 50)
    print("Restoring workspaces from remote mirror")
    print("=" * 50)

    if not test_ssh_connection(host):
        sys.exit(1)

    if snapshot:
        latest = find_latest_remote_folder(host, f"{base}_snapshots")
        remote_src = f"{latest}/"
    else:
        remote_src = f"{base}/"

    print(f"Remote : {host}:{remote_src}")
    print(f"Local  : {dest_path}")
    print("=" * 50)

    dest_path.mkdir(parents=True, exist_ok=True)
    print("Copying workspaces...")
    result = subprocess.run(
        ["rsync", "-avz", "--progress", *RSYNC_EXCLUDES, f"{host}:{remote_src}", f"{dest_path}/"],
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: rsync failed with exit code {result.returncode}")
        sys.exit(1)

    print("=" * 50)
    print("✓ Workspace restore complete!")
    print("=" * 50)
    print(f"Workspaces are now at: {dest_path}")


def _resolve_pull(source: str | None, dest: str | None, env_source: str, default_dest: str,
                  label: str) -> tuple[str, str, str]:
    """Resolve and validate a pull's remote source and local dest.

    Args:
        source: Explicit ``--source`` (remote host:/path), or None to use env.
        dest: Explicit ``--dest`` (local path), or None to use the default.
        env_source: Env var holding the remote default.
        default_dest: Resolved local destination default.
        label: Mode name for error messages (e.g. ``"--db"``).

    Returns:
        ``(host, remote_dir, local_dest)`` for a ``pull_*`` helper.
    """
    source = source or os.getenv(env_source)
    dest = dest or default_dest

    if not source:
        print(f"ERROR: {label} needs a remote source (set {env_source} or pass --source host:/path)")
        sys.exit(1)
    remote = parse_remote(source)
    if remote is None:
        print(f"ERROR: {label} --source must be a remote spec (format: host:/path)")
        sys.exit(1)
    if parse_remote(dest) is not None:
        print(f"ERROR: {label} --dest must be a LOCAL path (pull-only)")
        sys.exit(1)

    host, remote_dir = remote
    return host, remote_dir, dest


def _run_db_pull(source: str | None, dest: str | None, snapshot: bool) -> None:
    """Validate args for ``--db`` mode and pull the database."""
    host, remote_dir, dest = _resolve_pull(
        source, dest, "REMOTE_DB_PATH", os.getenv("DB_PATH", "./data/chats.db"), "--db",
    )
    pull_database(host, remote_dir, dest, snapshot=snapshot)


def _run_ws_pull(source: str | None, dest: str | None, snapshot: bool) -> None:
    """Validate args for the default workspace mode and pull the workspaces."""
    default_dest = os.getenv("FILE_TOOL_ROOT") or os.getenv("LOCAL_PATH", "agent_workspace")
    host, remote_dir, dest = _resolve_pull(
        source, dest, "REMOTE_PATH", default_dest, "workspace pull",
    )
    pull_workspaces(host, remote_dir, dest, snapshot=snapshot)


def _run_all_pull(snapshot: bool) -> None:
    """Pull BOTH the database and the workspaces from their remote mirrors.

    Uses .env directly (REMOTE_DB_PATH / DB_PATH and REMOTE_PATH /
    FILE_TOOL_ROOT) rather than ``--source``/``--dest``, since there are two
    of each.

    Args:
        snapshot: Restore both halves from their latest dated snapshot instead
            of the live mirror.
    """
    db_host, db_dir, db_dest = _resolve_pull(
        None, None, "REMOTE_DB_PATH", os.getenv("DB_PATH", "./data/chats.db"), "--all",
    )
    ws_default = os.getenv("FILE_TOOL_ROOT") or os.getenv("LOCAL_PATH", "agent_workspace")
    ws_host, ws_dir, ws_dest = _resolve_pull(
        None, None, "REMOTE_PATH", ws_default, "--all",
    )
    pull_database(db_host, db_dir, db_dest, snapshot=snapshot)
    pull_workspaces(ws_host, ws_dir, ws_dest, snapshot=snapshot)


def main():
    parser = argparse.ArgumentParser(
        description="Restore agent workspaces and/or the chats DB from a remote mirror (pull-only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Remote format: host:/path/to/dir   Local format: path/to/dir

Examples:
  %(prog)s
      # restore workspaces: REMOTE_PATH → FILE_TOOL_ROOT

  %(prog)s --snapshot
      # restore workspaces from the latest dated snapshot

  %(prog)s --db
      # restore database: REMOTE_DB_PATH → DB_PATH

  %(prog)s --all
      # restore BOTH: DB → DB_PATH and workspaces → FILE_TOOL_ROOT
        """
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--db",
        action="store_true",
        help="Pull the SQLite database from the remote mirror to local DB_PATH.",
    )
    mode.add_argument(
        "--all",
        action="store_true",
        help="Pull BOTH the database and workspaces (uses REMOTE_DB_PATH/DB_PATH and "
             "REMOTE_PATH/FILE_TOOL_ROOT from .env).",
    )
    parser.add_argument(
        "--source", "-s",
        default=None,
        help="Remote source override (host:/path). Default: REMOTE_PATH (workspaces) "
             "or REMOTE_DB_PATH (--db). Ignored by --all.",
    )
    parser.add_argument(
        "--dest", "-d",
        default=None,
        help="Local destination override. Default: FILE_TOOL_ROOT (workspaces) "
             "or DB_PATH (--db). Ignored by --all.",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="Restore from the latest dated snapshot instead of the live mirror.",
    )

    args = parser.parse_args()

    if args.all:
        _run_all_pull(args.snapshot)
    elif args.db:
        _run_db_pull(args.source, args.dest, args.snapshot)
    else:
        _run_ws_pull(args.source, args.dest, args.snapshot)


if __name__ == "__main__":
    main()
