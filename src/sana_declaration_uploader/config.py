"""Non-secret application configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    project_dir: Path
    inbox_dir: Path
    results_dir: Path

    @classmethod
    def for_project(cls, project_dir: Path) -> "AppConfig":
        root = project_dir.resolve()
        return cls(
            project_dir=root,
            inbox_dir=root / "inbox",
            results_dir=root / "results",
        )
