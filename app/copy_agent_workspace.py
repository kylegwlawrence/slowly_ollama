#!/usr/bin/env python3
"""
Copy agent workspace between local machine and a remote host via rsync.

Direction is inferred from which arg contains a colon (host:/path format):
  - Remote dest  → creates a timestamped backup folder on the remote host
  - Remote source → finds the latest timestamped folder and syncs it locally

Usage:
    python app/copy_agent_workspace.py --source <src> --dest <dest>

    Remote format : host:/path/to/dir
    Local format  : path/to/dir

Defaults are configured in .env (COPY_SOURCE, COPY_PIHOST, COPY_DEST).
Default behavior (no flags) copies local agent_workspace → remote host.

Examples:
    python app/copy_agent_workspace.py
    python app/copy_agent_workspace.py --dest raspberryweb-host:/home/user/agent_workspaces
    python app/copy_agent_workspace.py --source host1:/home/documents/projects/agent_workspaces --dest agent_workspace
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime
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
    """Return the full path of the most recent YYYYMMDD_HHMMSS folder on host."""
    print("Finding latest backup on remote...")
    cmd = ["ssh", host, f"ls -t {remote_base} | grep -E '^[0-9]{{8}}_[0-9]{{6}}$' | head -1"]
    result = run_command(cmd, "List remote folders")

    latest = result.stdout.strip()
    if not latest:
        print(f"ERROR: No timestamped folders found under {remote_base} on {host}")
        sys.exit(1)

    return f"{remote_base}/{latest}"


def copy_to_remote(local_source: str, host: str, remote_base: str, workspace: str | None = None) -> None:
    """Rsync local_source into a new timestamped folder on host.

    Args:
        local_source: Local base directory.
        host: Remote hostname.
        remote_base: Base path on remote where timestamped folders are created.
        workspace: Optional subfolder to copy instead of the full workspace.
    """
    src_path = Path(local_source) / workspace if workspace else Path(local_source)
    if not src_path.exists():
        print(f"ERROR: Source directory does not exist: {src_path}")
        sys.exit(1)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    remote_dest = f"{remote_base}/{timestamp}"
    if workspace:
        remote_dest = f"{remote_dest}/{workspace}"

    print("=" * 50)
    print("Copying workspace to remote host")
    print("=" * 50)
    print(f"Source : {src_path}")
    print(f"Dest   : {host}:{remote_dest}")
    print("=" * 50)

    if not test_ssh_connection(host):
        sys.exit(1)

    print("Creating destination directory...")
    run_command(["ssh", host, f"mkdir -p {remote_dest}"], "Directory creation")

    print("Copying files...")
    src = str(src_path) if str(src_path).endswith("/") else f"{src_path}/"
    result = subprocess.run(
        ["rsync", "-avz", "--progress", *RSYNC_EXCLUDES, src, f"{host}:{remote_dest}/"],
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: rsync failed with exit code {result.returncode}")
        sys.exit(1)

    print("=" * 50)
    print("✓ Copy complete!")
    print("=" * 50)
    print(f"Files are now at: {host}:{remote_dest}")
    print(f"\nTo verify: ssh {host} 'ls -la {remote_dest}'")


def copy_from_remote(host: str, remote_base: str, local_dest: str, workspace: str | None = None) -> None:
    """Rsync the latest timestamped folder from host into local_dest.

    Args:
        host: Remote hostname.
        remote_base: Base path on remote containing timestamped folders.
        local_dest: Local base directory to sync into.
        workspace: Optional subfolder to pull instead of the full workspace.
    """
    dest_path = Path(local_dest) / workspace if workspace else Path(local_dest)
    dest_path.mkdir(parents=True, exist_ok=True)

    print("=" * 50)
    print("Copying workspace from remote host")
    print("=" * 50)
    print(f"Remote base : {host}:{remote_base}")
    print(f"Local dest  : {dest_path}")
    print("=" * 50)

    if not test_ssh_connection(host):
        sys.exit(1)

    latest = find_latest_remote_folder(host, remote_base)
    remote_src = f"{latest}/{workspace}" if workspace else latest
    print(f"Latest backup: {host}:{remote_src}")

    print("Copying files...")
    remote_src_slash = remote_src if remote_src.endswith("/") else f"{remote_src}/"
    result = subprocess.run(
        ["rsync", "-avz", "--progress", *RSYNC_EXCLUDES, f"{host}:{remote_src_slash}", f"{dest_path}/"],
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: rsync failed with exit code {result.returncode}")
        sys.exit(1)

    print("=" * 50)
    print("✓ Copy complete!")
    print("=" * 50)
    print(f"Files are now at: {dest_path}")
    print(f"\nTo verify: ls -la {dest_path}")


def main():
    default_local = os.getenv("LOCAL_PATH", "agent_workspace")
    default_dest = os.getenv("REMOTE_PATH", "raspberryweb-host:/home/user/agent_workspaces")

    parser = argparse.ArgumentParser(
        description="Copy agent workspace between local machine and a remote host",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Remote format: host:/path/to/dir   Local format: path/to/dir

Examples:
  %(prog)s
      # local → remote using .env defaults

  %(prog)s --dest host1:/home/documents/projects/agent_workspaces
      # local agent_workspace → host1

  %(prog)s --source host1:/home/documents/projects/agent_workspaces --dest agent_workspace
      # latest backup on host1 → local agent_workspace

  %(prog)s --dest host1:/home/documents/projects/agent_workspaces --workspace physics-lessons
      # local agent_workspace/physics-lessons → host1 (timestamped)

  %(prog)s --source host1:/home/documents/projects/agent_workspaces --dest agent_workspace --workspace physics-lessons
      # physics-lessons from latest host1 backup → local agent_workspace/physics-lessons
        """
    )

    parser.add_argument(
        "--source", "-s",
        default=default_local,
        help=f"Source (local path or host:/path). Default: LOCAL_PATH or {default_local}",
    )
    parser.add_argument(
        "--dest", "-d",
        default=default_dest,
        help=f"Destination (local path or host:/path). Default: REMOTE_PATH or {default_dest}",
    )
    parser.add_argument(
        "--workspace", "-w",
        default=None,
        help="Subfolder to copy instead of the full workspace (e.g. physics-lessons)",
    )

    args = parser.parse_args()

    source_remote = parse_remote(args.source)
    dest_remote = parse_remote(args.dest)

    if source_remote and dest_remote:
        print("ERROR: both --source and --dest cannot be remote")
        sys.exit(1)
    if not source_remote and not dest_remote:
        print("ERROR: one of --source or --dest must be remote (format: host:/path)")
        sys.exit(1)

    if dest_remote:
        host, remote_base = dest_remote
        copy_to_remote(args.source, host, remote_base, workspace=args.workspace)
    else:
        host, remote_base = source_remote
        copy_from_remote(host, remote_base, args.dest, workspace=args.workspace)


if __name__ == "__main__":
    main()
