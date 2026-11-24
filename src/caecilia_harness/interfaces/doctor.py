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
    AGENT_NAMES,
    CONFIG_NAME,
    CliValues,
    ConfigError,
    NotAGitRepoError,
    RunOptions,
    project_settings,
    resolve_options,
)
from caecilia_harness.security import keys
from caecilia_harness.security.env import AGENT_PROVIDERS
from caecilia_harness.store.info import sqlite_supported, sqlite_version
from caecilia_harness.workers.external.profile import (
    AgentProfileError,
    detect_agent,
    profile_for,
)

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


def _agent_checks(options: RunOptions) -> list[Check]:
    """One line per configured agent: found, version, compatible; login is checked on first run."""
    names = sorted(set(options.agents))
    if options.worker in AGENT_NAMES:
        names = sorted({*names, options.worker})
    checks: list[Check] = []
    for name in names:
        config = options.agents.get(name)
        try:
            profile = profile_for(
                name,
                auth=config.auth if config is not None else None,
                model=config.model if config is not None else None,
                extra_args=tuple(config.extra_args) if config is not None else (),
            )
        except AgentProfileError as exc:
            checks.append(Check("agent", "fail", f"{name}: {exc}"))
            continue
        detection = detect_agent(profile)
        if not detection.found:
            checks.append(
                Check(
                    "agent",
                    "fail",
                    f"{name}: tidak ditemukan di PATH (program {profile.binary!r}; "
                    f"terms: {profile.terms_url or '-'})",
                )
            )
            continue
        version = detection.version or "tidak terbaca"
        cocok = "cocok" if detection.compatible else f"tidak cocok (butuh >= {profile.min_version})"
        checks.append(
            Check(
                "agent",
                "ok" if detection.compatible else "fail",
                f"{name}: ditemukan di {detection.path}, versi {version}, {cocok}; "
                "login dicek saat run pertama",
            )
        )
        if profile.auth == "api-key":
            provider = AGENT_PROVIDERS.get(name)
            if provider is None:
                checks.append(Check("api-key", "fail", f"{name}: tidak mendukung API key"))
                continue
            source = keys.key_source(provider)
            checks.append(
                Check(
                    "api-key",
                    "ok" if source else "warn",
                    f"key {provider} ada ({source})"
                    if source
                    else f"key {provider} tidak ada: `caecilia key set {provider}`",
                )
            )
    return checks


def check_project(cwd: Path) -> list[Check]:
    """`.caecilia.toml` of the working folder's repo, its API key and configured agents."""
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
    if options.worker == "fake":
        checks.append(Check("api-key", "ok", f"tidak dibutuhkan (worker {options.worker})"))
    elif options.worker == "native":
        source = keys.key_source(options.provider)
        if source is None:
            detail = (
                f"key {options.provider} tidak ada: "
                f"simpan dengan `caecilia key set {options.provider}`"
            )
            checks.append(Check("api-key", "warn", detail))
        else:
            checks.append(Check("api-key", "ok", f"key {options.provider} ada ({source})"))
    return [*checks, *_agent_checks(options)]


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
