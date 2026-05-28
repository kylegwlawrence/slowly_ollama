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
    python app/copy_agent_workspace.py --dest raspberrypi6:/home/user/agent_workspaces
    python app/copy_agent_workspace.py --source pop-os:/home/documents/projects/agent_workspaces --dest agent_workspace
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


def copy_to_remote(local_source: str, host: str, remote_base: str) -> None:
    """Rsync local_source into a new timestamped folder on host."""
    source_path = Path(local_source)
    if not source_path.exists():
        print(f"ERROR: Source directory does not exist: {local_source}")
        sys.exit(1)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    remote_dest = f"{remote_base}/{timestamp}"

    print("=" * 50)
    print("Copying workspace to remote host")
    print("=" * 50)
    print(f"Source : {local_source}")
    print(f"Dest   : {host}:{remote_dest}")
    print("=" * 50)

    if not test_ssh_connection(host):
        sys.exit(1)

    print("Creating destination directory...")
    run_command(["ssh", host, f"mkdir -p {remote_dest}"], "Directory creation")

    print("Copying files...")
    src = local_source if local_source.endswith("/") else f"{local_source}/"
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


def copy_from_remote(host: str, remote_base: str, local_dest: str) -> None:
    """Rsync the latest timestamped folder from host into local_dest."""
    Path(local_dest).mkdir(parents=True, exist_ok=True)

    print("=" * 50)
    print("Copying workspace from remote host")
    print("=" * 50)
    print(f"Remote base : {host}:{remote_base}")
    print(f"Local dest  : {local_dest}")
    print("=" * 50)

    if not test_ssh_connection(host):
        sys.exit(1)

    latest = find_latest_remote_folder(host, remote_base)
    print(f"Latest backup: {host}:{latest}")

    print("Copying files...")
    remote_src = latest if latest.endswith("/") else f"{latest}/"
    result = subprocess.run(
        ["rsync", "-avz", "--progress", *RSYNC_EXCLUDES, f"{host}:{remote_src}", f"{local_dest}/"],
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: rsync failed with exit code {result.returncode}")
        sys.exit(1)

    print("=" * 50)
    print("✓ Copy complete!")
    print("=" * 50)
    print(f"Files are now at: {local_dest}")
    print(f"\nTo verify: ls -la {local_dest}")


def main():
    default_local = os.getenv("COPY_SOURCE", "agent_workspace")
    default_pihost = os.getenv("COPY_PIHOST", "raspberrypi6")
    default_remote_base = os.getenv("COPY_DEST", "/home/user/agent_workspaces")
    default_dest = f"{default_pihost}:{default_remote_base}"

    parser = argparse.ArgumentParser(
        description="Copy agent workspace between local machine and a remote host",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Remote format: host:/path/to/dir   Local format: path/to/dir

Examples:
  %(prog)s
      # local → remote using .env defaults

  %(prog)s --dest pop-os:/home/documents/projects/agent_workspaces
      # local agent_workspace → pop-os

  %(prog)s --source pop-os:/home/documents/projects/agent_workspaces --dest agent_workspace
      # latest backup on pop-os → local agent_workspace
        """
    )

    parser.add_argument(
        "--source", "-s",
        default=default_local,
        help=f"Source (local path or host:/path). Default: {default_local}",
    )
    parser.add_argument(
        "--dest", "-d",
        default=default_dest,
        help=f"Destination (local path or host:/path). Default: {default_dest}",
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
        copy_to_remote(args.source, host, remote_base)
    else:
        host, remote_base = source_remote
        copy_from_remote(host, remote_base, args.dest)


if __name__ == "__main__":
    main()
