"""Where runtime data lives. Never inside the repository being worked on."""

from __future__ import annotations

import os
from pathlib import Path


def harness_home() -> Path:
    """Return CAECILIA_HOME (default: ~/.caecilia). Does not create it."""
    override = os.environ.get("CAECILIA_HOME")
    return Path(override).expanduser() if override else Path.home() / ".caecilia"


def db_path() -> Path:
    return harness_home() / "harness.db"


def worktrees_dir() -> Path:
    return harness_home() / "worktrees"


def artifacts_dir() -> Path:
    return harness_home() / "artifacts"


def logs_dir() -> Path:
    return harness_home() / "logs"
