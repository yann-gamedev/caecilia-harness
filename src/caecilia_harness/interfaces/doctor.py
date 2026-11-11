"""Environment checks for `caecilia doctor`."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

from caecilia_harness.paths import harness_home
from caecilia_harness.store.info import sqlite_supported, sqlite_version

Level = Literal["ok", "warn", "fail"]

MIN_PYTHON = (3, 12)
MIN_GIT = (2, 30)


@dataclass(frozen=True)
class Check:
    name: str
    level: Level
    detail: str


def _version_tuple(text: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not match:
        return ()
    return tuple(int(part) for part in match.groups() if part is not None)


def check_python() -> Check:
    current = sys.version_info[:2]
    level: Level = "ok" if current >= MIN_PYTHON else "fail"
    return Check("python", level, f"{platform.python_version()} (need >= 3.12)")


def check_git() -> Check:
    git = shutil.which("git")
    if git is None:
        return Check("git", "fail", "git not found on PATH")
    out = subprocess.run([git, "--version"], capture_output=True, text=True, check=False)
    version = _version_tuple(out.stdout)
    level: Level = "ok" if version[:2] >= MIN_GIT else "fail"
    return Check("git", level, f"{out.stdout.strip()} (need >= 2.30 for worktrees)")


def check_sqlite() -> Check:
    level: Level = "ok" if sqlite_supported() else "fail"
    return Check("sqlite", level, f"{sqlite_version()} (need >= 3.35)")


def check_sandbox() -> Check:
    system = platform.system()
    if system == "Linux":
        if shutil.which("bwrap"):
            return Check("sandbox", "ok", "bubblewrap available")
        return Check("sandbox", "warn", "bwrap not found: exec will always ask (ADR-0008)")
    return Check(
        "sandbox",
        "warn",
        f"no sandbox backend on {system} yet: exec will always ask unless --unsafe-no-sandbox",
    )


def check_home() -> Check:
    home = harness_home()
    state = "exists" if home.exists() else "will be created on first run"
    return Check("home", "ok", f"{home} ({state})")


def run_all() -> list[Check]:
    return [check_python(), check_git(), check_sqlite(), check_sandbox(), check_home()]
