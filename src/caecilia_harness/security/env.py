"""Remove secrets from the environment handed to worker commands (ADR-0008, zone Z2)."""

from __future__ import annotations

import re
from collections.abc import Mapping

_SECRET_NAME = re.compile(
    r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION", re.IGNORECASE
)

#: OS variables a child process needs to start at all. Kept even if they look secret-like.
ESSENTIAL = frozenset({"PATH", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE"})


def scrub_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of `environ` without variables whose name looks like a secret."""
    return {
        name: value
        for name, value in environ.items()
        # Windows env names are case-insensitive ("Path"), so compare upper-cased.
        if name.upper() in ESSENTIAL or not _SECRET_NAME.search(name)
    }
