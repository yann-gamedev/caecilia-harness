"""Contract every `WorkerAdapter` must pass (docs/architecture.md, ADR-0006, ADR-0018).

Subclass `WorkerContract` in this file (or in a new `test_*.py`) and provide four fixtures:

- `worker`: a ready worker that writes `expected_file` and succeeds;
- `slow_worker`: the same, but still working when `cancel()` is called;
- `failing_worker`: the same, but its task fails with a reason;
- `expected_file`: repo-relative path the worker writes in the task worktree.

The suite runs against `fake`, `native` (with `FakeProvider`) and `fake-agent` (a real child
process), so every adapter is held to the same behaviour.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from caecilia_harness.core.models import (
    Budget,
    ContextPack,
    ModelEvent,
    RunHandle,
    TaskSpec,
    WorkerEvent,
    Workspace,
)
from caecilia_harness.core.ports import WorkerAdapter
from caecilia_harness.providers.fake import FakeProvider
from caecilia_harness.security.approval import ApprovalPolicy, ExecRequest
from caecilia_harness.security.env import agent_env
from caecilia_harness.workers.external.base import ExternalAgentWorker
from caecilia_harness.workers.external.profile import profile_for
from caecilia_harness.workers.fake import FakeWorker
from caecilia_harness.workers.native.worker import NativeWorker
from caecilia_harness.workspace.exec_runner import ExecRunner
from caecilia_harness.workspace.git_worktree import GitWorktreeBackend
from tests.integration.conftest import git

SPEC = TaskSpec(
    id="t1",
    run_id="r1",
    title="write a file",
    instructions="Write the requested file.",
    acceptance=["the file exists"],
    write_set=["**"],
)
CANCEL_TIMEOUT_S = 15.0


async def _yes(req: ExecRequest) -> bool:
    return True


def _done(reason: str = "end_turn") -> ModelEvent:
    return ModelEvent(kind="done", data={"stop_reason": reason})


def _native(provider: FakeProvider) -> NativeWorker:
    def factory(ws: Workspace) -> ExecRunner:
        return ExecRunner(Path(ws.path), ApprovalPolicy(), _yes, None, task_id=ws.task_id)

    return NativeWorker(provider, "fake-1", factory, retry_backoff_s=0)


def _agent(mode: str = "ok", *, echo: str | None = None) -> ExternalAgentWorker:
    profile = profile_for("fake-agent")
    env = {**agent_env(profile, os.environ), "FAKE_AGENT_MODE": mode}
    if echo is not None:
        env["FAKE_AGENT_ECHO"] = echo
    return ExternalAgentWorker(
        profile, env=env, grace_s=0.5, secrets=(echo,) if echo is not None else ()
    )


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        tasklist = shutil.which("tasklist")
        assert tasklist is not None
        done = subprocess.run(
            [tasklist, "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in done.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _drain(worker: WorkerAdapter, handle: RunHandle) -> list[WorkerEvent]:
    return [event async for event in worker.events(handle)]


async def _start(worker: WorkerAdapter, ws: Workspace) -> RunHandle:
    return await worker.start(SPEC, ContextPack(task=SPEC), ws, Budget())


async def _cancelled(worker: WorkerAdapter, ws: Workspace, mode: str) -> None:
    handle = await _start(worker, ws)
    # Let the worker enter its work loop: cancelling a task that never ran is a different case.
    await asyncio.sleep(0.1)
    await worker.cancel(handle, "hard" if mode == "hard" else "soft")
    result = await asyncio.wait_for(worker.result(handle), CANCEL_TIMEOUT_S)
    assert not result.success
    assert result.error == "cancelled"


class WorkerContract:
    """Base class of the worker contract suite. Not collected on its own (no `Test` prefix)."""

    @pytest.fixture
    async def workspace(self, tmp_path: Path) -> AsyncIterator[Workspace]:
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.name", "Test User")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "config", "core.autocrlf", "false")
        (repo / "README.md").write_text("# demo\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "initial")
        backend = GitWorktreeBackend(repo)
        ws = await backend.create(SPEC.id, "HEAD")
        try:
            yield ws
        finally:
            await backend.cleanup(ws, keep=False)

    async def test_is_a_worker_adapter(self, worker: WorkerAdapter) -> None:
        assert isinstance(worker, WorkerAdapter)

    async def test_start_events_and_result(
        self, worker: WorkerAdapter, workspace: Workspace, expected_file: str
    ) -> None:
        handle = await _start(worker, workspace)
        assert handle.task_id == SPEC.id
        assert handle.worker == worker.id
        assert handle.attempt >= 1

        events = await _drain(worker, handle)
        result = await worker.result(handle)

        assert result.success, result.error
        assert result.task_id == SPEC.id
        assert (Path(workspace.path) / expected_file).is_file()
        assert events, "worker yielded no events"
        assert events[-1].kind == "progress"
        assert events[-1].data["status"] == "finished"

    async def test_diff_is_computed_from_worktree(
        self, worker: WorkerAdapter, workspace: Workspace, expected_file: str
    ) -> None:
        result = await worker.result(await _start(worker, workspace))
        assert result.success, result.error
        diff = await GitWorktreeBackend(Path(workspace.path)).capture_diff(workspace)
        assert diff.files_changed == [expected_file]
        assert diff.head_commit != diff.base_commit

    async def test_soft_cancel_finishes_as_cancelled(
        self, slow_worker: WorkerAdapter, workspace: Workspace
    ) -> None:
        await _cancelled(slow_worker, workspace, "soft")

    async def test_hard_cancel_finishes_as_cancelled(
        self, slow_worker: WorkerAdapter, workspace: Workspace
    ) -> None:
        await _cancelled(slow_worker, workspace, "hard")

    async def test_failure_carries_a_reason(
        self, failing_worker: WorkerAdapter, workspace: Workspace
    ) -> None:
        handle = await _start(failing_worker, workspace)
        result = await asyncio.wait_for(failing_worker.result(handle), CANCEL_TIMEOUT_S)
        assert not result.success
        assert result.error


class TestFakeWorkerContract(WorkerContract):
    @pytest.fixture
    def worker(self) -> FakeWorker:
        return FakeWorker(files={"fake.txt": "hello\n"})

    @pytest.fixture
    def slow_worker(self) -> FakeWorker:
        return FakeWorker(files={"fake.txt": "hello\n"}, delay_s=5.0)

    @pytest.fixture
    def failing_worker(self) -> FakeWorker:
        return FakeWorker(files={"fake.txt": "hello\n"}, fail_with="fake_failed")

    @pytest.fixture
    def expected_file(self) -> str:
        return "fake.txt"


class TestNativeWorkerContract(WorkerContract):
    @pytest.fixture
    def worker(self) -> NativeWorker:
        provider = FakeProvider(
            [
                [
                    FakeProvider.tool_call(
                        "c1", "write_file", {"path": "native.txt", "content": "hi\n"}
                    ),
                    _done("tool_use"),
                ],
                [FakeProvider.text("wrote native.txt"), FakeProvider.usage(10, 5), _done()],
            ]
        )
        return _native(provider)

    @pytest.fixture
    def slow_worker(self) -> NativeWorker:
        turn = [FakeProvider.text(str(index)) for index in range(5)] + [
            FakeProvider.tool_call("c1", "read_file", {"path": "README.md"}),
            _done("tool_use"),
        ]
        return _native(FakeProvider([turn, [_done()]], delay=0.05))

    @pytest.fixture
    def failing_worker(self) -> NativeWorker:
        error = ModelEvent(kind="error", data={"message": "boom", "retryable": False})
        return _native(FakeProvider([[error]]))

    @pytest.fixture
    def expected_file(self) -> str:
        return "native.txt"


class TestFakeAgentWorkerContract(WorkerContract):
    @pytest.fixture
    def worker(self) -> ExternalAgentWorker:
        return _agent()

    @pytest.fixture
    def slow_worker(self) -> ExternalAgentWorker:
        return _agent("hang")

    @pytest.fixture
    def failing_worker(self) -> ExternalAgentWorker:
        return _agent("fail")

    @pytest.fixture
    def expected_file(self) -> str:
        return "FAKE_AGENT.md"

    async def test_crashing_agent_fails_with_a_reason(self, workspace: Workspace) -> None:
        worker = _agent("crash")
        result = await asyncio.wait_for(
            worker.result(await _start(worker, workspace)), CANCEL_TIMEOUT_S
        )
        assert not result.success
        assert result.error is not None and "agent_exited" in result.error

    async def test_agent_without_a_result_line_fails(self, workspace: Workspace) -> None:
        worker = _agent("no-result")
        result = await asyncio.wait_for(
            worker.result(await _start(worker, workspace)), CANCEL_TIMEOUT_S
        )
        assert not result.success
        assert result.error is not None and "agent_no_result" in result.error

    async def test_secret_like_output_is_redacted(self, workspace: Workspace) -> None:
        canary = "sk-test-CANARY-EXT-01"
        worker = _agent(echo=canary)
        handle = await _start(worker, workspace)
        events = await _drain(worker, handle)
        result = await worker.result(handle)
        assert result.success, result.error

        log = worker.log_text(handle)
        assert canary not in log
        assert "***" in log
        assert all(canary not in json.dumps(event.data) for event in events)

    async def test_cancelled_agent_leaves_no_orphan_process(self, workspace: Workspace) -> None:
        worker = _agent("hang")
        handle = await _start(worker, workspace)
        assert handle.pid is not None
        await worker.cancel(handle, "hard")
        result = await asyncio.wait_for(worker.result(handle), CANCEL_TIMEOUT_S)
        assert result.error == "cancelled"
        assert not _pid_alive(handle.pid)
