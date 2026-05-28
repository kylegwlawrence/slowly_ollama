#!/usr/bin/env python3
"""
Copy the latest agent workspace from Raspberry Pi to local machine via rsync.

Finds the most recent timestamped folder under COPY_DEST on the Pi and
rsyncs its contents to the local COPY_SOURCE directory.

Usage:
    python app/copy_agent_workspace_from_pi.py [local_dest] [pihost] [remote_base]

Defaults are configured in .env (COPY_SOURCE, COPY_PIHOST, COPY_DEST)

Example:
    python app/copy_agent_workspace_from_pi.py  # uses defaults from .env
    # Copies: raspberryweb-host:/home/user/agent_workspaces/<latest>/ → agent_workspace/
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def run_command(cmd: list[str], description: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command and handle errors."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=check)
        return result
    except subprocess.CalledProcessError as e:
        print(f"ERROR: {description} failed")
        print(f"Command: {' '.join(cmd)}")
        print(f"Error: {e.stderr}")
        sys.exit(1)


def test_ssh_connection(pihost: str) -> bool:
    """Test SSH connectivity to the Pi."""
    print("Testing SSH connection...")
    cmd = ["ssh", "-o", "ConnectTimeout=5", pihost, "echo 'Connection successful'"]
    result = run_command(cmd, "SSH connection test", check=False)

    if result.returncode != 0:
        print(f"ERROR: Cannot connect to {pihost}")
        print("Please check:")
        print("  1. Raspberry Pi is powered on")
        print("  2. SSH is enabled")
        print("  3. SSH config has correct hostname/username")
        return False

    print(result.stdout.strip())
    return True


def find_latest_remote_folder(pihost: str, remote_base: str) -> str:
    """Return the path of the most recent timestamped subfolder on the Pi."""
    print("Finding latest backup on Pi...")
    # ls -t lists newest-first; grep restricts to YYYYMMDD_HHMMSS folders only
    cmd = ["ssh", pihost, f"ls -t {remote_base} | grep -E '^[0-9]{{8}}_[0-9]{{6}}$' | head -1"]
    result = run_command(cmd, "List remote folders")

    latest = result.stdout.strip()
    if not latest:
        print(f"ERROR: No folders found under {remote_base} on {pihost}")
        sys.exit(1)

    return f"{remote_base}/{latest}"


def rsync_files(pihost: str, remote_path: str, local_dest: str) -> None:
    """Rsync files from the Pi to local_dest with progress."""
    print("Copying files...")

    # Trailing slash on source copies contents, not the folder itself
    remote_source = remote_path if remote_path.endswith("/") else f"{remote_path}/"

    cmd = [
        "rsync",
        "-avz",
        "--progress",
        "--exclude", ".DS_Store",
        "--exclude", "__pycache__",
        "--exclude", "*.pyc",
        "--exclude", ".pytest_cache",
        "--exclude", ".coverage",
        f"{pihost}:{remote_source}",
        f"{local_dest}/",
    ]

    result = subprocess.run(cmd, text=True)

    if result.returncode != 0:
        print(f"ERROR: rsync failed with exit code {result.returncode}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Copy the latest agent workspace from Raspberry Pi to local machine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          # use defaults from .env
  %(prog)s my_local_folder          # custom local dest, default host and remote base
  %(prog)s my_folder mypi /opt      # all custom
        """
    )

    default_local_dest = os.getenv("COPY_SOURCE", "agent_workspace")
    default_pihost = os.getenv("COPY_PIHOST", "raspberryweb-host")
    default_remote_base = os.getenv("COPY_DEST", "/home/user/agent_workspaces")

    parser.add_argument(
        "local_dest",
        nargs="?",
        default=default_local_dest,
        help=f"Local directory to copy files into (default: {default_local_dest})"
    )
    parser.add_argument(
        "pihost",
        nargs="?",
        default=default_pihost,
        help=f"Raspberry Pi hostname (default: {default_pihost})"
    )
    parser.add_argument(
        "remote_base",
        nargs="?",
        default=default_remote_base,
        help=f"Base directory on Pi containing timestamped backups (default: {default_remote_base})"
    )

    args = parser.parse_args()

    # Ensure local destination exists
    local_path = Path(args.local_dest)
    local_path.mkdir(parents=True, exist_ok=True)

    print("=" * 50)
    print("Copying workspace from Raspberry Pi")
    print("=" * 50)
    print(f"Remote base: {args.pihost}:{args.remote_base}")
    print(f"Local dest:  {args.local_dest}")
    print("=" * 50)

    if not test_ssh_connection(args.pihost):
        sys.exit(1)

    latest_remote = find_latest_remote_folder(args.pihost, args.remote_base)
    print(f"Latest backup: {args.pihost}:{latest_remote}")

    rsync_files(args.pihost, latest_remote, args.local_dest)

    print("=" * 50)
    print("✓ Copy complete!")
    print("=" * 50)
    print(f"Files are now at: {args.local_dest}")
    print()
    print("To verify, run:")
    print(f"  ls -la {args.local_dest}")


if __name__ == "__main__":
    main()
