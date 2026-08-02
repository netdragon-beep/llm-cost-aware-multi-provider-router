"""Native launcher entry point bundled by PyInstaller for both desktop platforms."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def packaged_root(*, is_windows: bool | None = None) -> Path:
    windows = sys.platform == "win32" if is_windows is None else is_windows
    executable = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False) and not windows:
        return executable.parents[1] / "Resources" / "app"
    return executable.parent


def runtime_environment(root: Path, *, is_windows: bool) -> dict[str, str]:
    environment = os.environ.copy()
    environment["RELAYDECK_ENV_ROOT"] = str(root / "runtime")
    return environment


def bootstrap_configuration(root: Path) -> None:
    config_root = root / "config"
    for template_name, target_name in (
        ("litellm.yaml.example", "litellm.yaml"),
        ("relaydeck-state.example.json", "relaydeck-state.json"),
    ):
        template = config_root / template_name
        target = config_root / target_name
        if template.is_file() and not target.exists():
            target.write_bytes(template.read_bytes())


def command_for(root: Path, *, is_windows: bool, install_browser_runtime: bool) -> list[str]:
    script_name = "install-browser-runtime" if install_browser_runtime else "start-all"
    if is_windows:
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(root / "scripts" / f"{script_name}.ps1")]
    return ["/bin/sh", str(root / "scripts" / f"{script_name}.sh")]


def main() -> int:
    is_windows = sys.platform == "win32"
    root = packaged_root(is_windows=is_windows)
    install_browser_runtime = "--install-browser-runtime" in sys.argv[1:]
    bootstrap_configuration(root)
    command = command_for(root, is_windows=is_windows, install_browser_runtime=install_browser_runtime)
    return subprocess.run(command, cwd=root, env=runtime_environment(root, is_windows=is_windows), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
