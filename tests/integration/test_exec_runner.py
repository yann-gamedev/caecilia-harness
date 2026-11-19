"""Exec runner: approval flow, timeout, output limit (backlog M1-06)."""

from __future__ import annotations

import asyncio
import gc
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from caecilia_harness.security.approval import ApprovalPolicy, ExecRequest
from caecilia_harness.workspace import exec_runner
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
    slots = asyncio.Semaphore(0)
    approver = RecordingApprover(True)
    runner = _runner(worktree, approver, ApprovalPolicy(mode="deny"), slots=slots)
    result = await asyncio.wait_for(runner.run(_marker_cmd("m")), 5)
    assert result.denied
    assert approver.calls == []
    assert not (worktree / "m").exists()
    assert slots._value == 0


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
    slots = asyncio.Semaphore(1)
    cmd = [PY, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"]
    result = await _runner(worktree, RecordingApprover(True), slots=slots).run(cmd)
    assert (result.exit_code, result.stderr_tail, result.timed_out) == (3, "bad", False)
    assert slots._value == 1


async def test_timeout_kills_process_tree(worktree: Path) -> None:
    slots = asyncio.Semaphore(1)
    runner = _runner(worktree, RecordingApprover(True), timeout_s=1, slots=slots)
    start = time.monotonic()
    result = await runner.run([PY, "-c", "import time; time.sleep(30)"])
    assert result.timed_out
    assert time.monotonic() - start < 10
    assert slots._value == 1


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


async def test_cancelling_run_kills_the_process_tree(
    worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slots = asyncio.Semaphore(1)
    started = asyncio.Event()
    spawn = asyncio.create_subprocess_exec

    async def observe_spawn(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await spawn(*args, **kwargs)
        started.set()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    runner = _runner(worktree, RecordingApprover(True), slots=slots)
    late = "import time; time.sleep(2); open('late.txt', 'w').close()"
    task = asyncio.create_task(runner.run([PY, "-c", late]))
    await asyncio.wait_for(started.wait(), 5)
    assert slots._value == 0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slots._value == 1
    await asyncio.sleep(3)
    assert not (worktree / "late.txt").exists()


async def test_cancel_holds_slot_until_process_is_reaped(
    worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slots = asyncio.Semaphore(1)
    started = asyncio.Event()
    killed = asyncio.Event()
    reaping = asyncio.Event()
    release = asyncio.Event()
    spawn = asyncio.create_subprocess_exec
    kill_tree = exec_runner._kill_tree

    async def observe_spawn(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await spawn(*args, **kwargs)
        if args[0] != PY:
            return proc
        wait = proc.wait

        async def held_wait() -> int:
            code = await wait()
            reaping.set()
            await release.wait()
            return code

        monkeypatch.setattr(proc, "wait", held_wait)
        started.set()
        return proc

    async def observe_kill(proc: asyncio.subprocess.Process) -> None:
        await kill_tree(proc)
        killed.set()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    monkeypatch.setattr(exec_runner, "_kill_tree", observe_kill)
    runner = _runner(worktree, RecordingApprover(True), slots=slots)
    task = asyncio.create_task(runner.run([PY, "-c", "import time; time.sleep(30)"]))
    reap_task = asyncio.create_task(reaping.wait())
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.wait_for(killed.wait(), 5)
        await asyncio.wait_for(
            asyncio.wait({task, reap_task}, return_when=asyncio.FIRST_COMPLETED), 5
        )
        assert not task.done(), "Cancellation returned before process wait completed"
        assert slots._value == 0
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "Repeated cancellation interrupted cleanup"
        assert slots._value == 0
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(reap_task, 5)
    assert slots._value == 1


async def test_cancel_retrieves_reader_cancellation(
    worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    errors: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()
    started = asyncio.Event()
    spawn = asyncio.create_subprocess_exec

    async def observe_spawn(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await spawn(*args, **kwargs)
        started.set()
        return proc

    async def cancel_run() -> None:
        runner = _runner(worktree, RecordingApprover(True), slots=asyncio.Semaphore(1))
        task = asyncio.create_task(runner.run([PY, "-c", "import time; time.sleep(30)"]))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        await cancel_run()
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
        assert errors == []
    finally:
        loop.set_exception_handler(previous_handler)


async def test_shared_slot_serializes_commands(
    worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slots = asyncio.Semaphore(1)
    first_started = asyncio.Event()
    second_approved = asyncio.Event()
    release = asyncio.Event()
    calls: list[tuple[str, ...]] = []
    spawn = asyncio.create_subprocess_exec

    async def held_spawn(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        calls.append(args)
        proc = await spawn(*args, **kwargs)
        if len(calls) == 1:
            wait = proc.wait

            async def held_wait() -> int:
                first_started.set()
                await release.wait()
                return await wait()

            monkeypatch.setattr(proc, "wait", held_wait)
        return proc

    async def approve(req: ExecRequest) -> bool:
        if req.task_id == "second":
            second_approved.set()
        return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", held_spawn)
    first = ExecRunner(worktree, ApprovalPolicy(), approve, None, slots=slots)
    second = ExecRunner(worktree, ApprovalPolicy(), approve, None, task_id="second", slots=slots)
    tasks = [asyncio.create_task(first.run(_marker_cmd("first")))]
    try:
        await asyncio.wait_for(first_started.wait(), 5)
        tasks.append(asyncio.create_task(second.run(_marker_cmd("second"))))
        await asyncio.wait_for(second_approved.wait(), 5)
        assert len(calls) == 1
        assert slots._value == 0
    finally:
        release.set()
        results = await asyncio.gather(*tasks)
    assert len(calls) == 2
    assert all(result.exit_code == 0 for result in results)
    assert slots._value == 1


async def test_waiting_approval_does_not_hold_exec_slot(worktree: Path) -> None:
    slots = asyncio.Semaphore(1)
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def approve(req: ExecRequest) -> bool:
        if req.task_id == "waiting":
            waiting.set()
            await release.wait()
        return True

    first = ExecRunner(worktree, ApprovalPolicy(), approve, None, task_id="waiting", slots=slots)
    second = ExecRunner(worktree, ApprovalPolicy(), approve, None, slots=slots)
    task = asyncio.create_task(first.run(_marker_cmd("first")))
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        assert slots._value == 1
        result = await asyncio.wait_for(second.run(_marker_cmd("second")), 5)
        assert result.exit_code == 0
        assert not task.done()
    finally:
        release.set()
        await task
    assert slots._value == 1


async def test_spawn_failure_releases_exec_slot(worktree: Path) -> None:
    slots = asyncio.Semaphore(1)
    runner = _runner(worktree, RecordingApprover(True), slots=slots)
    with pytest.raises(FileNotFoundError):
        await runner.run([str(worktree / "missing-executable")])
    assert slots._value == 1
    assert (await runner.run(_marker_cmd("after-error"))).exit_code == 0


async def test_cancelling_slot_wait_does_not_release_another_commands_slot(worktree: Path) -> None:
    slots = asyncio.Semaphore(0)
    approved = asyncio.Event()

    async def approve(req: ExecRequest) -> bool:
        approved.set()
        return True

    runner = ExecRunner(worktree, ApprovalPolicy(), approve, None, slots=slots)
    task = asyncio.create_task(runner.run(_marker_cmd("cancelled")))
    await asyncio.wait_for(approved.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slots._value == 0
    assert not (worktree / "cancelled").exists()
