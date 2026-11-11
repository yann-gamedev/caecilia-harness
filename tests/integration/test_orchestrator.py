"""Orchestrator run loop end to end with FakeWorker and a real git repo (backlog M1-10)."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from caecilia_harness.artifacts import read_bytes
from caecilia_harness.bootstrap import ProviderNotConfiguredError, artifact_info, build_orchestrator
from caecilia_harness.core.models import Action, GateReport, TaskSpec
from caecilia_harness.core.orchestrator import Orchestrator
from caecilia_harness.core.ports import UserPrompter
from caecilia_harness.core.states import TaskState
from caecilia_harness.gate.simple import SimpleGate
from caecilia_harness.paths import db_path, worktrees_dir
from caecilia_harness.store.db import open_db
from caecilia_harness.store.events import SqliteEventStore
from caecilia_harness.store.projections import project_runs_and_tasks, rebuild_projections
from caecilia_harness.workers.fake import FakeWorker
from caecilia_harness.workspace.git_worktree import GitWorktreeBackend
from tests.integration.conftest import git

PY = sys.executable
PASS = [PY, "-c", "print('all good')"]
FAIL = [PY, "-c", "import sys; print('check failed: lint'); sys.exit(1)"]


class FakePrompter:
    def __init__(self, approve: bool = True, merge: bool = True) -> None:
        self.approve = approve
        self.merge = merge
        self.approvals: list[Action] = []
        self.merges: list[GateReport] = []

    async def ask_approval(self, action: Action) -> bool:
        self.approvals.append(action)
        return self.approve

    async def confirm_merge(self, report: GateReport) -> bool:
        self.merges.append(report)
        return self.merge


@pytest.fixture
def store() -> Iterator[SqliteEventStore]:
    s = SqliteEventStore(open_db(db_path()), projectors=[project_runs_and_tasks])
    try:
        yield s
    finally:
        s.close()


def _orchestrator(
    store: SqliteEventStore,
    repo: Path,
    worker: FakeWorker,
    check: list[str] = PASS,
    prompter: FakePrompter | None = None,
    auto_merge: bool = True,
) -> Orchestrator:
    prompter = prompter or FakePrompter()
    gate = SimpleGate(repo, check, confirm=prompter.confirm_merge, auto_merge=auto_merge)
    return Orchestrator(
        store,
        GitWorktreeBackend(repo),
        gate,
        {"fake": worker},
        prompter,
        artifact_info=artifact_info,
    )


def _spec(task_id: str = "t1") -> TaskSpec:
    return TaskSpec(
        id=task_id, run_id="r1", title="add notes", instructions="Add notes.", worker="fake"
    )


def _create(orch: Orchestrator, repo: Path, spec: TaskSpec | None = None) -> str:
    return orch.create_run(
        "add notes", str(repo), git(repo, "rev-parse", "main"), spec or _spec(), "main"
    )


def _types(store: SqliteEventStore, task_id: str = "t1") -> list[str]:
    return [e.type for e in store.read_stream(f"task:{task_id}")]


def _states(store: SqliteEventStore, task_id: str = "t1") -> list[str]:
    return [
        e.payload["to_state"]
        for e in store.read_stream(f"task:{task_id}")
        if e.type == "task.state_changed"
    ]


def _run_status(store: SqliteEventStore) -> str:
    [finished] = [e for e in store.read_stream("run:r1") if e.type == "run.finished"]
    return str(finished.payload["status"])


async def test_happy_path(store: SqliteEventStore, git_repo: Path) -> None:
    worker = FakeWorker(files={"NOTES.md": "notes\n"})
    orch = _orchestrator(store, git_repo, worker)
    run_id = _create(orch, git_repo)

    await orch.run(run_id)

    assert store.get_task("t1").state is TaskState.DONE
    assert git(git_repo, "show", "main:NOTES.md") == "notes"
    assert _types(store) == [
        "task.created",
        "task.state_changed",  # PENDING -> READY
        "task.state_changed",  # READY -> LEASED
        "task.lease_acquired",
        "worker.started",
        "task.state_changed",  # LEASED -> RUNNING
        "worker.result",
        "task.state_changed",  # RUNNING -> AWAITING_GATE
        "task.lease_released",
        "task.state_changed",  # AWAITING_GATE -> INTEGRATING
        "gate.started",
        "gate.merging",
        "gate.passed",
        "task.state_changed",  # INTEGRATING -> DONE
    ]
    assert _states(store) == [
        "READY",
        "LEASED",
        "RUNNING",
        "AWAITING_GATE",
        "INTEGRATING",
        "DONE",
    ]
    assert _run_status(store) == "done"
    assert not (worktrees_dir() / "t1").exists()  # cleaned up after DONE
    [result] = [e for e in store.read_stream("task:t1") if e.type == "worker.result"]
    patch = result.payload["diff"]["patch_ref"]
    row = store._conn.execute(
        "SELECT kind, task_id, bytes FROM artifacts WHERE hash = ?", (patch,)
    ).fetchone()
    assert tuple(row) == ("patch", "t1", len(read_bytes(patch)))


async def test_failed_check_is_reworked_with_gate_report(
    store: SqliteEventStore, git_repo: Path, tmp_path: Path
) -> None:
    counter = tmp_path / "gate-runs.txt"
    flaky = [
        PY,
        "-c",
        "import pathlib, sys\n"
        f"p = pathlib.Path({str(counter)!r})\n"
        "n = int(p.read_text()) if p.exists() else 0\n"
        "p.write_text(str(n + 1))\n"
        "if n == 0:\n"
        "    print('first check failed: missing docs'); sys.exit(1)\n",
    ]
    worker = FakeWorker(files={"NOTES.md": "notes\n"})
    orch = _orchestrator(store, git_repo, worker, check=flaky)
    await orch.run(_create(orch, git_repo))

    task = store.get_task("t1")
    assert (task.state, task.attempts) == (TaskState.DONE, 2)
    assert "REWORK" in _states(store)
    assert worker.contexts[0].handoff_notes == []
    assert any("missing docs" in note for note in worker.contexts[1].handoff_notes)
    assert git(git_repo, "show", "main:NOTES.md") == "notes"


async def test_check_failing_forever_needs_human(store: SqliteEventStore, git_repo: Path) -> None:
    worker = FakeWorker(files={"NOTES.md": "notes\n"})
    old = git(git_repo, "rev-parse", "main")
    orch = _orchestrator(store, git_repo, worker, check=FAIL)
    await orch.run(_create(orch, git_repo))

    task = store.get_task("t1")
    assert (task.state, task.attempts) == (TaskState.NEEDS_HUMAN, 3)
    assert len(worker.contexts) == 3
    assert git(git_repo, "rev-parse", "main") == old
    assert _run_status(store) == "needs_human"


async def test_permanent_worker_failure_keeps_worktree(
    store: SqliteEventStore, git_repo: Path
) -> None:
    worker = FakeWorker(files={"NOTES.md": "x"}, fail_with="permanent")
    orch = _orchestrator(store, git_repo, worker)
    await orch.run(_create(orch, git_repo))

    assert store.get_task("t1").state is TaskState.FAILED
    assert (worktrees_dir() / "t1" / "NOTES.md").exists()
    assert _run_status(store) == "failed"


async def test_transient_failure_is_retried(store: SqliteEventStore, git_repo: Path) -> None:
    worker = FakeWorker(files={"NOTES.md": "x"}, fail_with="transient")
    orch = _orchestrator(store, git_repo, worker)
    await orch.run(_create(orch, git_repo))

    task = store.get_task("t1")
    assert (task.state, task.attempts) == (TaskState.FAILED, 3)
    assert _states(store).count("RETRY_WAIT") == 2


async def test_declined_merge_needs_human(store: SqliteEventStore, git_repo: Path) -> None:
    old = git(git_repo, "rev-parse", "main")
    prompter = FakePrompter(merge=False)
    orch = _orchestrator(
        store, git_repo, FakeWorker(files={"N.md": "n"}), prompter=prompter, auto_merge=False
    )
    await orch.run(_create(orch, git_repo))

    assert store.get_task("t1").state is TaskState.NEEDS_HUMAN
    assert len(prompter.merges) == 1
    assert git(git_repo, "rev-parse", "main") == old


async def test_outbox_spawn_is_idempotent(
    store: SqliteEventStore, git_repo: Path, tmp_path: Path
) -> None:
    log = tmp_path / "spawn.log"
    orch = _orchestrator(store, git_repo, FakeWorker(files={"N.md": "n"}, spawn_log=log))
    run_id = _create(orch, git_repo)
    while store.get_task("t1").state is not TaskState.LEASED:
        await orch.advance("t1")

    [item] = store.pending_outbox()
    await orch.process_outbox()
    # Simulate a crash after the spawn but before the item was marked done.
    store.mark_outbox(item.id, "pending")
    await orch.process_outbox()

    assert log.read_text(encoding="utf-8").splitlines() == ["t1:1"]
    assert store.pending_outbox() == []
    await orch.run(run_id)
    assert store.get_task("t1").state is TaskState.DONE


async def test_rebuild_projections_after_run(store: SqliteEventStore, git_repo: Path) -> None:
    orch = _orchestrator(store, git_repo, FakeWorker(files={"N.md": "n"}))
    await orch.run(_create(orch, git_repo))
    conn = store._conn
    before = [tuple(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")]
    rebuild_projections(conn)
    assert [tuple(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")] == before


async def test_exec_approval_is_recorded(store: SqliteEventStore, git_repo: Path) -> None:
    prompter = FakePrompter(approve=False)
    orch = _orchestrator(store, git_repo, FakeWorker(), prompter=prompter)
    action = Action(kind="exec", task_id="t1", detail={"argv": ["make"], "cwd": "."})

    assert await orch.approve(action) is False
    assert prompter.approvals == [action]
    events = store.read_stream("approval:t1")
    assert [e.type for e in events] == ["approval.requested", "approval.decided"]
    assert events[1].payload["allowed"] is False
    assert events[1].causation_id == events[0].event_id


async def test_bootstrap_fake_run(git_repo: Path) -> None:
    prompter = FakePrompter()
    assert isinstance(prompter, UserPrompter)
    orch = build_orchestrator(git_repo, "fake", PASS, prompter, auto_merge=True)
    try:
        spec = _spec().model_copy(update={"instructions": "write the demo note"})
        await orch.run(_create(orch, git_repo, spec))
        assert orch.store.get_task("t1").state is TaskState.DONE
        assert git(git_repo, "show", "main:CAECILIA_FAKE.md") == "write the demo note"
    finally:
        orch.close()


def test_bootstrap_native_needs_provider_decision(git_repo: Path) -> None:
    with pytest.raises(ProviderNotConfiguredError, match="D2"):
        build_orchestrator(git_repo, "native", PASS, FakePrompter())
