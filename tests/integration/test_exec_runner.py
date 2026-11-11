"""Exec runner: approval flow, timeout, output limit (backlog M1-06)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from caecilia_harness.security.approval import ApprovalPolicy, ExecRequest
from caecilia_harness.workspace.exec_runner import ExecRefusedError, ExecRunner
from caecilia_harness.workspace.sandbox import BubblewrapSandbox

PY = sys.executable


class RecordingApprover:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.calls: list[ExecRequest] = []

    async def __call__(self, req: ExecRequest) -> bool:
        self.calls.append(req)
        return self.answer


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "work tree"
    (wt / "sub").mkdir(parents=True)
    return wt


def _runner(
    worktree: Path,
    approver: RecordingApprover,
    policy: ApprovalPolicy | None = None,
    **kw: object,
) -> ExecRunner:
    return ExecRunner(worktree, policy or ApprovalPolicy(), approver, None, **kw)  # type: ignore[arg-type]


def _marker_cmd(name: str) -> list[str]:
    """A command that leaves `name` in its cwd, so tests can tell whether it ran."""
    return [PY, "-c", f"open({name!r}, 'w').close(); print('ran')"]


async def test_deny_runs_nothing(worktree: Path) -> None:
    approver = RecordingApprover(True)
    runner = _runner(worktree, approver, ApprovalPolicy(mode="deny"))
    result = await runner.run(_marker_cmd("m"))
    assert result.denied
    assert approver.calls == []
    assert not (worktree / "m").exists()


async def test_ask_rejected_by_approver(worktree: Path) -> None:
    approver = RecordingApprover(False)
    result = await _runner(worktree, approver).run(_marker_cmd("m"))
    assert result.denied
    assert len(approver.calls) == 1
    assert not (worktree / "m").exists()


async def test_ask_approved_runs(worktree: Path) -> None:
    approver = RecordingApprover(True)
    result = await _runner(worktree, approver).run(_marker_cmd("m"), cwd=worktree / "sub")
    assert not result.denied
    assert result.exit_code == 0
    assert result.stdout_tail.strip() == "ran"
    assert not result.sandboxed
    assert (worktree / "sub" / "m").exists()
    [req] = approver.calls
    assert req.cwd == (worktree / "sub").resolve()
    assert req.argv == _marker_cmd("m")


async def test_allow_without_sandbox_still_asks(worktree: Path) -> None:
    approver = RecordingApprover(True)
    await _runner(worktree, approver, ApprovalPolicy(mode="allow")).run(_marker_cmd("m"))
    assert len(approver.calls) == 1


async def test_allow_with_unsafe_no_sandbox_does_not_ask(worktree: Path) -> None:
    approver = RecordingApprover(False)
    runner = _runner(worktree, approver, ApprovalPolicy(mode="allow"), unsafe_no_sandbox=True)
    result = await runner.run(_marker_cmd("m"))
    assert approver.calls == []
    assert result.exit_code == 0


async def test_remembered_prefix_skips_approver(worktree: Path) -> None:
    approver = RecordingApprover(False)
    policy = ApprovalPolicy(remembered={(PY, "-c")})
    result = await _runner(worktree, approver, policy).run(_marker_cmd("m"))
    assert approver.calls == []
    assert result.exit_code == 0


async def test_nonzero_exit_and_stderr(worktree: Path) -> None:
    cmd = [PY, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"]
    result = await _runner(worktree, RecordingApprover(True)).run(cmd)
    assert (result.exit_code, result.stderr_tail, result.timed_out) == (3, "bad", False)


async def test_timeout_kills_process_tree(worktree: Path) -> None:
    runner = _runner(worktree, RecordingApprover(True), timeout_s=1)
    start = time.monotonic()
    result = await runner.run([PY, "-c", "import time; time.sleep(30)"])
    assert result.timed_out
    assert time.monotonic() - start < 10


async def test_output_is_truncated_to_limit(worktree: Path) -> None:
    runner = _runner(worktree, RecordingApprover(True), output_limit=100)
    cmd = [PY, "-c", "import sys; sys.stdout.write('a' * 5000 + 'END')"]
    result = await runner.run(cmd)
    assert len(result.stdout_tail.encode()) <= 100
    assert result.stdout_tail.endswith("END")


@pytest.mark.parametrize("argv", [[], [""], "echo hi", ["echo", 1]])
async def test_invalid_argv_is_refused(worktree: Path, argv: object) -> None:
    approver = RecordingApprover(True)
    with pytest.raises(ExecRefusedError):
        await _runner(worktree, approver).run(argv)  # type: ignore[arg-type]
    assert approver.calls == []


def test_bubblewrap_mounts_tmpfs_before_worktree(tmp_path: Path) -> None:
    wt = Path("/tmp/wt")  # noqa: S108 - argv construction only, nothing is written
    argv = BubblewrapSandbox("/usr/bin/bwrap").wrap(["make"], wt, wt)
    assert argv.index("--tmpfs") < argv.index("--bind")
    assert "--unshare-net" in argv
    assert argv[-2:] == ["--", "make"]
    assert argv[argv.index("--chdir") + 1] == str(wt)


async def test_cancelling_run_kills_the_process_tree(worktree: Path) -> None:
    import asyncio

    runner = _runner(worktree, RecordingApprover(True))
    late = "import time; time.sleep(2); open('late.txt', 'w').close()"
    task = asyncio.create_task(runner.run([PY, "-c", late]))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(3)
    assert not (worktree / "late.txt").exists()
