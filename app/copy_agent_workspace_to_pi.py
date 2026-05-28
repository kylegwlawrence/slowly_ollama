#!/usr/bin/env python3
"""
Copy agent workspace to Raspberry Pi via rsync with timestamped backups.

Each run creates a new timestamped folder within dest/YYYYMMDD_HHMMSS/
to preserve old versions.

Usage:
    python app/copy_agent_workspace_to_pi.py [source] [pihost] [dest]

Defaults are configured in .env (COPY_SOURCE, COPY_PIHOST, COPY_DEST)

Example:
    python app/copy_agent_workspace_to_pi.py  # uses defaults from .env
    # Creates: /home/user/agent_workspaces/20260527_143022/
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Load environment variables from .env
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


def create_dest_directory(pihost: str, dest: str) -> None:
    """Create destination directory on the Pi."""
    print("Creating destination directory...")
    cmd = ["ssh", pihost, f"mkdir -p {dest}"]
    run_command(cmd, "Directory creation")


def rsync_files(source: str, pihost: str, dest: str) -> None:
    """Rsync files to the Pi with progress and exclusions."""
    print("Copying files...")

    # Ensure source ends with / to copy contents, not the folder itself
    source_path = source if source.endswith('/') else f"{source}/"

    cmd = [
        "rsync",
        "-avz",
        "--progress",
        "--exclude", ".DS_Store",
        "--exclude", "__pycache__",
        "--exclude", "*.pyc",
        "--exclude", ".pytest_cache",
        "--exclude", ".coverage",
        f"{source_path}",
        f"{pihost}:{dest}/"
    ]

    result = subprocess.run(cmd, text=True)

    if result.returncode != 0:
        print(f"ERROR: rsync failed with exit code {result.returncode}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Copy agent workspace to Raspberry Pi via rsync",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                          # use defaults from .env
  %(prog)s --source my_folder                       # custom source
  %(prog)s --host mypi --dest /opt/backups          # custom host and dest
  %(prog)s --source my_folder --host mypi --dest /opt/backups
        """
    )

    # Get defaults from environment variables
    default_source = os.getenv("COPY_SOURCE", "agent_workspace")
    default_pihost = os.getenv("COPY_PIHOST", "raspberrypi6")
    default_dest = os.getenv("COPY_DEST", "/home/user/agent_workspaces")

    parser.add_argument(
        "--source", "-s",
        default=default_source,
        help=f"Source directory to copy (default: {default_source})"
    )
    parser.add_argument(
        "--host",
        default=default_pihost,
        help=f"Raspberry Pi hostname (default: {default_pihost})"
    )
    parser.add_argument(
        "--dest", "-d",
        default=default_dest,
        help=f"Destination path on Pi (default: {default_dest})"
    )

    args = parser.parse_args()

    # Validate source exists
    source_path = Path(args.source)
    if not source_path.exists():
        print(f"ERROR: Source directory does not exist: {args.source}")
        sys.exit(1)

    # Create timestamped destination path
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamped_dest = f"{args.dest}/{timestamp}"

    print("=" * 50)
    print("Copying workspace to Raspberry Pi")
    print("=" * 50)
    print(f"Source: {args.source}")
    print(f"Destination: {args.host}:{timestamped_dest}")
    print("=" * 50)

    # Test SSH connection
    if not test_ssh_connection(args.host):
        sys.exit(1)

    # Create destination directory
    create_dest_directory(args.host, timestamped_dest)

    # Rsync files
    rsync_files(args.source, args.host, timestamped_dest)

    print("=" * 50)
    print("✓ Copy complete!")
    print("=" * 50)
    print(f"Files are now at: {args.host}:{timestamped_dest}")
    print()
    print("To verify, run:")
    print(f"  ssh {args.host} 'ls -la {timestamped_dest}'")


if __name__ == "__main__":
    main()
