"""Run git as an async subprocess. Arguments are always passed as a list, never a shell string."""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


class GitError(Exception):
    """git is missing or exited with a non-zero status."""

    def __init__(self, message: str, returncode: int | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: bytes
    stderr: str

    @property
    def text(self) -> str:
        """stdout decoded as UTF-8 with trailing whitespace removed."""
        return self.stdout.decode("utf-8", errors="replace").rstrip()


def git_executable() -> str:
    exe = shutil.which("git")
    if exe is None:
        raise GitError("git executable not found on PATH")
    return exe


async def run_git(args: list[str], cwd: Path, *, check: bool = True) -> GitResult:
    """Run `git <args>` in `cwd`. Raise `GitError` on a non-zero exit when `check` is True."""
    env = dict(os.environ)
    # Never block on a credential or passphrase prompt: there is no terminal to answer it.
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = await asyncio.create_subprocess_exec(
        git_executable(),
        *args,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr_bytes = await proc.communicate()
    assert proc.returncode is not None  # noqa: S101 - communicate() waits for exit
    result = GitResult(proc.returncode, stdout, stderr_bytes.decode("utf-8", errors="replace"))
    if check and result.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}",
            result.returncode,
            result.stderr,
        )
    return result
