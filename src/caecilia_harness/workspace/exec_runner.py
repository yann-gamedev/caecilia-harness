"""The only way a worker runs a command (zone Z2, ADR-0008).

Every command is confined to the task worktree, goes through the approval policy, gets a
scrubbed environment and its own process group, and is killed as a tree on timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from caecilia_harness.security.approval import ApprovalPolicy, Approver, ExecRequest
from caecilia_harness.security.env import scrub_env
from caecilia_harness.workspace.sandbox import SandboxBackend

# After killing a timed-out tree, how long to wait for its pipes to close.
_KILL_GRACE_S = 5.0


class ExecRefusedError(Exception):
    """The request itself is invalid (bad argv, cwd outside the worktree). Nothing was run."""


class ExecResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int | None
    stdout_tail: str
    stderr_tail: str
    timed_out: bool = False
    denied: bool = False
    sandboxed: bool = False


async def _read_tail(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    """Drain `stream`, keeping only its last `limit` bytes in memory."""
    if stream is None:
        return b""
    tail = bytearray()
    while chunk := await stream.read(65_536):
        tail += chunk
        if len(tail) > limit:
            del tail[:-limit]
    return bytes(tail)


def _decode_tail(data: bytes, limit: int) -> str:
    return data[-limit:].decode("utf-8", errors="replace") if limit > 0 else ""


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill `proc` and every process it started."""
    if sys.platform == "win32":
        taskkill = shutil.which("taskkill")
        if taskkill is not None:
            killer = await asyncio.create_subprocess_exec(
                taskkill,
                "/T",
                "/F",
                "/PID",
                str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
    else:
        # The child leads its own session, so its pgid equals its pid.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


class ExecRunner:
    """Run commands for one task inside `worktree`."""

    def __init__(
        self,
        worktree: Path,
        policy: ApprovalPolicy,
        approver: Approver,
        sandbox: SandboxBackend | None,
        unsafe_no_sandbox: bool = False,
        timeout_s: float = 600,
        output_limit: int = 64_000,
        task_id: str = "",
    ) -> None:
        self.worktree = worktree.resolve()
        self.policy = policy
        self.approver = approver
        self.sandbox = sandbox if sandbox is not None and sandbox.available() else None
        self.unsafe_no_sandbox = unsafe_no_sandbox
        self.timeout_s = timeout_s
        self.output_limit = output_limit
        self.task_id = task_id

    def _resolve_cwd(self, cwd: Path | None) -> Path:
        if cwd is None:
            return self.worktree
        target = (cwd if cwd.is_absolute() else self.worktree / cwd).resolve()
        if not target.is_relative_to(self.worktree):
            raise ExecRefusedError(f"cwd {target} is outside the worktree {self.worktree}")
        if not target.is_dir():
            raise ExecRefusedError(f"cwd {target} is not a directory")
        return target

    async def _approved(self, req: ExecRequest) -> bool:
        decision = self.policy.decide(req)
        if decision == "deny":
            return False
        # ADR-0008: a blanket `allow` is never honoured without a sandbox unless the user opted
        # out. A prefix a person explicitly remembered is their own approval, so it counts.
        trusted = self.sandbox or self.unsafe_no_sandbox or self.policy.remembers(req.argv)
        if decision == "allow" and trusted:
            return True
        return await self.approver(req)

    async def run(self, argv: list[str], cwd: Path | None = None) -> ExecResult:
        """Run `argv` (a list, never a shell string) after the policy and approver agree."""
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(arg, str) for arg in argv)
            or not argv[0]
        ):
            raise ExecRefusedError(f"argv must be a non-empty list of strings: {argv!r}")
        workdir = self._resolve_cwd(cwd)
        req = ExecRequest(argv=argv, cwd=workdir, task_id=self.task_id)
        if not await self._approved(req):
            return ExecResult(exit_code=None, stdout_tail="", stderr_tail="", denied=True)

        final_argv = argv
        if self.sandbox is not None:
            final_argv = self.sandbox.wrap(argv, self.worktree, workdir)

        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_exec(
            *final_argv,
            cwd=workdir,
            env=scrub_env(os.environ),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        readers = asyncio.gather(
            _read_tail(proc.stdout, self.output_limit),
            _read_tail(proc.stderr, self.output_limit),
        )
        timed_out = False
        try:
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), self.timeout_s)
            except TimeoutError:
                timed_out = True
                await _kill_tree(proc)
            try:
                stdout, stderr = await asyncio.wait_for(readers, _KILL_GRACE_S)
            except TimeoutError:
                # A grandchild escaped the kill and still holds a pipe; give up on its output.
                readers.cancel()
                stdout, stderr = b"", b""
            exit_code = await proc.wait()
        except asyncio.CancelledError:
            # The caller was cancelled (e.g. a hard worker cancel): never leave the tree running.
            readers.cancel()
            await asyncio.shield(_kill_tree(proc))
            raise
        return ExecResult(
            exit_code=exit_code,
            stdout_tail=_decode_tail(stdout, self.output_limit),
            stderr_tail=_decode_tail(stderr, self.output_limit),
            timed_out=timed_out,
            sandboxed=self.sandbox is not None,
        )
