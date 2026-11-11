"""OS sandboxes that confine worker commands to their worktree (ADR-0008).

Only Linux/bubblewrap exists in M1. Elsewhere `detect_sandbox()` returns None and the exec
runner falls back to asking a person for every command.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class SandboxBackend(Protocol):
    def wrap(self, argv: list[str], worktree: Path, cwd: Path) -> list[str]:
        """Return the argv that runs `argv` in `cwd` confined to `worktree`."""
        ...

    def available(self) -> bool: ...


class BubblewrapSandbox:
    """Read-only host, writable worktree, private /tmp, no network."""

    def __init__(self, bwrap: str | None = None) -> None:
        self._bwrap = bwrap or shutil.which("bwrap")

    def available(self) -> bool:
        return self._bwrap is not None

    def wrap(self, argv: list[str], worktree: Path, cwd: Path) -> list[str]:
        if self._bwrap is None:
            raise RuntimeError("bubblewrap (bwrap) is not installed")
        wt = str(worktree)
        return [
            self._bwrap,
            "--ro-bind", "/", "/",
            # --tmpfs /tmp must come before --bind <wt>: worktrees (tests especially) often
            # live under /tmp and would otherwise be hidden by the fresh tmpfs.
            "--tmpfs", "/tmp",  # noqa: S108 - a private tmpfs inside the sandbox
            "--bind", wt, wt,
            "--dev", "/dev",
            "--proc", "/proc",
            "--unshare-net",
            "--die-with-parent",
            "--chdir", str(cwd),
            "--",
            *argv,
        ]  # fmt: skip


def detect_sandbox() -> SandboxBackend | None:
    """Return the sandbox for this platform, or None if there is none."""
    if sys.platform.startswith("linux"):
        sandbox = BubblewrapSandbox()
        if sandbox.available():
            return sandbox
    return None
