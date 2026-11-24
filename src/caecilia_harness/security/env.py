"""Remove secrets from the environment handed to worker commands (ADR-0008, zone Z2).

The same scrubbing applies to an external agent process (EXT-01): it inherits only what it needs
to start, and the vendor key variables are decided by the profile's auth mode, not by whatever
happens to be in the user's shell (ADR-0018 point 2).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caecilia_harness.workers.external.profile import AgentProfile

_SECRET_NAME = re.compile(
    r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION", re.IGNORECASE
)

#: OS variables a child process needs to start at all. Kept even if they look secret-like.
ESSENTIAL = frozenset({"PATH", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE"})

#: Locale/timezone variables an agent CLI may read; they carry no credentials.
LOCALE_VARS = ("LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "LC_TIME", "TZ")

#: Agent name -> the provider id whose key `caecilia key set` stores for it.
AGENT_PROVIDERS: dict[str, str] = {
    "claude-code": "anthropic",
    "codex": "openai",
    "gemini-cli": "gemini",
}

#: Agent name -> the environment variable the vendor's own CLI reads its API key from.
VENDOR_KEY_ENV: dict[str, str] = {
    "claude-code": "ANTHROPIC_API_KEY",
    "codex": "OPENAI_API_KEY",
    "gemini-cli": "GEMINI_API_KEY",
}

#: Every vendor key variable, removed for `auth = "subscription"` so the agent uses its login.
ALL_VENDOR_KEY_ENVS = frozenset(VENDOR_KEY_ENV.values())


class AgentEnvError(ValueError):
    """The agent environment cannot be built (unknown agent or a missing key)."""


def scrub_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of `environ` without variables whose name looks like a secret."""
    return {
        name: value
        for name, value in environ.items()
        # Windows env names are case-insensitive ("Path"), so compare upper-cased.
        if name.upper() in ESSENTIAL or not _SECRET_NAME.search(name)
    }


def agent_env(
    profile: AgentProfile,
    environ: Mapping[str, str],
    api_key: str | None = None,
) -> dict[str, str]:
    """The environment for one agent process, built from the profile's auth mode.

    - always: `scrub_env` (so no secret-like variable survives), plus locale variables;
    - `auth = "subscription"`: every vendor key variable is removed, even one the scrubber
      would keep, so the agent falls back to its own login;
    - `auth = "api-key"`: exactly one variable is added, the vendor's official name.
    """
    env = scrub_env(environ)
    for name in LOCALE_VARS:
        if name in environ:
            env[name] = environ[name]
    for name in ALL_VENDOR_KEY_ENVS:
        env.pop(name, None)
    if profile.auth == "subscription":
        return env
    variable = VENDOR_KEY_ENV.get(profile.name)
    if variable is None:
        raise AgentEnvError(f"agent {profile.name} tidak punya variabel API key resmi")
    if not api_key:
        raise AgentEnvError(f"API key untuk {profile.name} belum ada")
    env[variable] = api_key
    return env
