"""Environment checks for `caecilia doctor`."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from caecilia_harness.paths import harness_home
from caecilia_harness.project_config import (
    CONFIG_NAME,
    CliValues,
    ConfigError,
    NotAGitRepoError,
    project_settings,
    resolve_options,
)
from caecilia_harness.security import keys
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


def check_on_path() -> Check:
    """Is `caecilia` callable from any folder (ADR-0015)?"""
    found = shutil.which("caecilia")
    if found:
        return Check("path", "ok", f"caecilia ada di PATH ({found})")
    return Check(
        "path",
        "warn",
        "caecilia belum ada di PATH: pasang dengan `uv tool install --editable .` di repo harness "
        "(lalu `uv tool update-shell` kalau perlu)",
    )


def check_project(cwd: Path) -> list[Check]:
    """`.caecilia.toml` of the working folder's repo, and the API key its provider needs."""
    try:
        root, config = project_settings(cwd)
    except NotAGitRepoError:
        return [
            Check("config", "ok", "folder kerja bukan repo Git; cek konfigurasi proyek dilewati")
        ]
    except ConfigError as exc:
        return [Check("config", "fail", str(exc))]
    if config is None:
        checks = [
            Check("config", "ok", f"tidak ada {CONFIG_NAME} di {root} (opsional; `caecilia init`)")
        ]
    else:
        checks = [Check("config", "ok", f"{root / CONFIG_NAME} valid")]
    try:
        options = resolve_options(CliValues(), config)
    except ValueError as exc:
        return [Check("config", "fail", str(exc))]
    if options.worker != "native":
        return [*checks, Check("api-key", "ok", f"tidak dibutuhkan (worker {options.worker})")]
    source = keys.key_source(options.provider)
    if source is None:
        detail = (
            f"key {options.provider} tidak ada: simpan dengan `caecilia key set {options.provider}`"
        )
        return [*checks, Check("api-key", "warn", detail)]
    return [*checks, Check("api-key", "ok", f"key {options.provider} ada ({source})")]


def run_all(cwd: Path | None = None) -> list[Check]:
    return [
        check_python(),
        check_git(),
        check_sqlite(),
        check_sandbox(),
        check_home(),
        check_on_path(),
        *check_project(cwd if cwd is not None else Path.cwd()),
    ]
