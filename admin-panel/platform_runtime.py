from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    platform_name: str
    env_root: Path
    python_executable: Path

    @classmethod
    def from_platform(
        cls,
        *,
        root: Path,
        platform_name: str | None = None,
        env_root: Path | None = None,
        python_executable: Path | None = None,
    ) -> "RuntimePaths":
        name = platform_name or sys.platform
        configured_root = os.environ.get("RELAYDECK_ENV_ROOT", "").strip()
        default_root = root / ".venv" if name != "win32" else Path(r"D:/conda/envs/llm-stack-local")
        resolved_root = env_root or (Path(configured_root) if configured_root else default_root)
        default_python = resolved_root / ("python.exe" if name == "win32" else "bin/python")
        return cls(root, name, resolved_root, python_executable or default_python)

    @property
    def is_windows(self) -> bool:
        return self.platform_name == "win32"

    @property
    def script_suffix(self) -> str:
        return ".ps1" if self.is_windows else ".sh"

    def script_path(self, name: str) -> Path:
        return self.root / "scripts" / f"{name}{self.script_suffix}"

    def tool_path(self, name: str) -> Path:
        if self.is_windows:
            return self.env_root / "Scripts" / f"{name}.exe"
        return self.env_root / "bin" / name

    def claude_code_settings_path(self, home: Path | None = None) -> Path:
        return (home or Path.home()) / ".claude" / "settings.json"


__all__ = ["RuntimePaths"]
