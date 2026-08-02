"""Create the source payload used by the native platform installers."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path


PAYLOAD_DIRECTORIES = ("admin-panel", "quota-adapters", "scripts")
PAYLOAD_FILES = (
    ".env.example",
    "claude_desktop_gateway.py",
    "relaydeck_litellm_callback.py",
    "relaydeck_litellm_discovery.py",
    "requirements.txt",
    "sitecustomize.py",
)
IGNORED_DIRECTORY_NAMES = {"__pycache__", "data", "logs", "run", "tmp"}
IGNORED_FILE_SUFFIXES = {".pyc", ".pyo"}


def should_copy(path: Path) -> bool:
    return path.name not in IGNORED_DIRECTORY_NAMES and path.suffix not in IGNORED_FILE_SUFFIXES


def copy_payload(source_root: Path, destination_root: Path) -> None:
    """Copy only distributable application files, never user state or credentials."""
    source_root = source_root.resolve()
    destination_root.mkdir(parents=True, exist_ok=True)

    for directory_name in PAYLOAD_DIRECTORIES:
        source_directory = source_root / directory_name
        if source_directory.is_dir():
            shutil.copytree(source_directory, destination_root / directory_name, ignore=shutil.ignore_patterns(*IGNORED_DIRECTORY_NAMES, "*.pyc", "*.pyo"))

    source_config = source_root / "config"
    if source_config.is_dir():
        destination_config = destination_root / "config"
        destination_config.mkdir(exist_ok=True)
        for config_file in source_config.iterdir():
            if config_file.is_file() and (config_file.name.endswith(".example.yaml") or config_file.name.endswith(".example.json") or config_file.name == "litellm-claude.yaml"):
                shutil.copy2(config_file, destination_config / config_file.name)

    for file_name in PAYLOAD_FILES:
        source_file = source_root / file_name
        if source_file.is_file():
            shutil.copy2(source_file, destination_root / file_name)


def checkout_git_head(source_root: Path, destination_root: Path) -> None:
    """Extract the committed source so local edits and untracked state cannot ship."""
    archive = subprocess.run(
        ["git", "-C", str(source_root), "archive", "--format=tar", "HEAD"],
        check=True,
        capture_output=True,
    )
    with tempfile.NamedTemporaryFile(suffix=".tar") as archive_file:
        archive_file.write(archive.stdout)
        archive_file.flush()
        with tarfile.open(archive_file.name) as tar:
            destination_root = destination_root.resolve()
            for member in tar.getmembers():
                target = (destination_root / member.name).resolve()
                if destination_root not in target.parents and target != destination_root:
                    raise ValueError(f"Refusing unsafe archive member: {member.name}")
                tar.extract(member, destination_root)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("destination_root", type=Path)
    parser.add_argument("--from-git-head", action="store_true")
    arguments = parser.parse_args()

    if arguments.destination_root.exists():
        shutil.rmtree(arguments.destination_root)

    if arguments.from_git_head:
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshot_root = Path(temporary_directory) / "snapshot"
            snapshot_root.mkdir()
            checkout_git_head(arguments.source_root, snapshot_root)
            copy_payload(snapshot_root, arguments.destination_root)
    else:
        copy_payload(arguments.source_root, arguments.destination_root)


if __name__ == "__main__":
    main()
