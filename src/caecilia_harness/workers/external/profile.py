"""Profiles of the external agent CLIs the harness may drive (EXT-01, ADR-0018).

A profile is data only: which program to start, the oldest version whose headless output the
adapter understands, how the agent authenticates, and where its terms live. The harness never
reads the agent's configuration folder, credential files or keychain; detection runs the
program's own `--version` and nothing else (ADR-0018 point 2).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from caecilia_harness.project_config import AGENT_NAMES, AgentName, AuthMode
from caecilia_harness.security.env import scrub_env

_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
#: Env var with the path of the fake agent program, for demos and tests.
FAKE_AGENT_ENV = "CAECILIA_FAKE_AGENT"
#: Agents whose subscription login the owner has not approved yet (decision D14).
SUBSCRIPTION_BLOCKED = ("codex", "gemini-cli")


class AgentProfileError(ValueError):
    """The profile cannot be used (unknown agent, refused auth mode, missing fake program)."""


@dataclass(frozen=True)
class AgentProfile:
    """Everything the harness needs to know about one agent CLI, and nothing secret."""

    name: AgentName
    binary: str
    min_version: str
    auth: AuthMode
    model: str | None = None
    extra_args: tuple[str, ...] = ()
    #: Vendor page that states whether the agent may be driven by another program.
    terms_url: str = ""
    #: CLI version whose headless output was checked (prompt 37-38); empty until verified.
    verified_version: str = ""


@dataclass(frozen=True)
class AgentDetection:
    """What `detect_agent` found: presence, version and whether it is new enough."""

    profile: AgentProfile
    found: bool
    path: str | None = None
    version: str | None = None
    compatible: bool = False


#: Registry of the profiles the harness ships. Auth defaults follow decision D14: only Claude
#: Code may use its own subscription login today.
AGENT_PROFILES: dict[AgentName, AgentProfile] = {
    "claude-code": AgentProfile(
        name="claude-code",
        binary="claude",
        min_version="1.0.0",
        auth="subscription",
        terms_url="https://code.claude.com/docs/en/legal-and-compliance.md",
    ),
    "codex": AgentProfile(
        name="codex",
        binary="codex",
        min_version="0.1.0",
        auth="api-key",
        terms_url="https://openai.com/policies/terms-of-use/",
    ),
    "gemini-cli": AgentProfile(
        name="gemini-cli",
        binary="gemini",
        min_version="0.1.0",
        auth="api-key",
        terms_url="https://policies.google.com/terms",
    ),
    "fake-agent": AgentProfile(
        name="fake-agent",
        binary=sys.executable,
        min_version="0.1.0",
        auth="subscription",
        verified_version="0.1.0",
    ),
}


def fake_agent_script() -> Path:
    """The fake agent program: `CAECILIA_FAKE_AGENT`, else `tests/fixtures` of this checkout."""
    override = os.environ.get(FAKE_AGENT_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "fake_agent_cli.py"


def profile_for(
    name: str,
    *,
    auth: AuthMode | None = None,
    model: str | None = None,
    extra_args: tuple[str, ...] = (),
) -> AgentProfile:
    """The profile for `name`, with the config's overrides applied.

    Raises `AgentProfileError` for an unknown agent, for a subscription login the owner has not
    approved (D14), or when the fake agent program is missing.
    """
    if name not in AGENT_PROFILES:
        raise AgentProfileError(
            f"agent tidak dikenal: {name!r} (tersedia: {', '.join(AGENT_NAMES)})"
        )
    profile = AGENT_PROFILES[name]
    chosen_auth = auth or profile.auth
    if name in SUBSCRIPTION_BLOCKED and chosen_auth == "subscription":
        raise AgentProfileError(
            f'langganan untuk {name} belum diizinkan (keputusan D14): pakai auth = "api-key"'
        )
    args = extra_args
    if name == "fake-agent":
        script = fake_agent_script()
        if not script.is_file():
            raise AgentProfileError(
                f"program fake agent tidak ditemukan: {script} "
                f"(set {FAKE_AGENT_ENV} ke path tests/fixtures/fake_agent_cli.py)"
            )
        args = (str(script), *extra_args)
    return replace(
        profile,
        auth=chosen_auth,
        model=model if model is not None else profile.model,
        extra_args=(*profile.extra_args, *args),
    )


def parse_version(text: str) -> tuple[int, int, int] | None:
    """First `X.Y[.Z]` in `text` as a comparable tuple, or None."""
    match = _VERSION.search(text)
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def _which(binary: str) -> str | None:
    found = shutil.which(binary)
    if found is not None or os.name != "nt":
        return found
    # Windows resolves PATHEXT itself, but be explicit about the suffixes agents ship with.
    for suffix in (".exe", ".cmd", ".bat"):
        found = shutil.which(binary + suffix)
        if found is not None:
            return found
    return None


def detect_agent(profile: AgentProfile, timeout_s: float = 10.0) -> AgentDetection:
    """Find the binary on PATH and ask it for its version. Never reads any config file."""
    path = _which(profile.binary)
    if path is None:
        return AgentDetection(profile=profile, found=False)
    try:
        done = subprocess.run(
            [
                path,
                *([str(fake_agent_script())] if profile.name == "fake-agent" else []),
                "--version",
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            check=False,
            env=scrub_env(os.environ),
        )
    except (OSError, subprocess.TimeoutExpired):
        return AgentDetection(profile=profile, found=True, path=path)
    version = parse_version(f"{done.stdout} {done.stderr}")
    minimum = parse_version(profile.min_version) or (0, 0, 0)
    compatible = done.returncode == 0 and version is not None and version >= minimum
    text = ".".join(str(part) for part in version) if version is not None else None
    return AgentDetection(
        profile=profile, found=True, path=path, version=text, compatible=compatible
    )


__all__ = [
    "AGENT_PROFILES",
    "FAKE_AGENT_ENV",
    "AgentDetection",
    "AgentProfile",
    "AgentProfileError",
    "detect_agent",
    "fake_agent_script",
    "parse_version",
    "profile_for",
]
