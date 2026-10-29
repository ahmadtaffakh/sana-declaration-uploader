"""Non-secret application configuration."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


def lookup_directory(
    *,
    module_file: Path | None = None,
    bundle_dir: Path | None = None,
) -> Path:
    """Return the lookup catalog directory for source and frozen runs."""

    frozen_root = bundle_dir or getattr(sys, "_MEIPASS", None)
    if frozen_root is not None:
        return Path(frozen_root).resolve() / "lookups"
    source_file = (module_file or Path(__file__)).resolve()
    return source_file.parents[2] / "lookups"


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
