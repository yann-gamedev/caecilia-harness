"""Parallel scheduler: max_parallel, write-set conflicts, serial gate (backlog M2-03, ADR-0013)."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from caecilia_harness.bootstrap import artifact_info, build_orchestrator
from caecilia_harness.core.models import (
    Action,
    Actor,
    Budget,
    ContextPack,
    DiffArtifact,
    GateReport,
    NewEvent,
    RunHandle,
    TaskSpec,
    TaskStateChange,
    WorkerResult,
    Workspace,
)
from caecilia_harness.core.orchestrator import Orchestrator
from caecilia_harness.core.ports import BeforeMerge
from caecilia_harness.core.prompting import SerializedPrompter
from caecilia_harness.core.states import TaskState
from caecilia_harness.gate.simple import SimpleGate
from caecilia_harness.paths import db_path
from caecilia_harness.store.db import open_db
from caecilia_harness.store.events import SqliteEventStore
from caecilia_harness.store.projections import project_runs_and_tasks
from caecilia_harness.workers.fake import FakeWorker
from caecilia_harness.workspace.git_worktree import GitWorktreeBackend
from tests.integration.conftest import git

S = TaskState
PASS = [sys.executable, "-c", "print('ok')"]


class Prompter:
    async def ask_approval(self, action: Action) -> bool:
        return True

    async def confirm_merge(self, report: GateReport) -> bool:
        return True


class SlowStartWorker(FakeWorker):
    """`start` takes a while, as a real worker's process start does."""

    async def start(
        self, spec: TaskSpec, ctx: ContextPack, ws: Workspace, budget: Budget
    ) -> RunHandle:
        await asyncio.sleep(0.2)
        return await super().start(spec, ctx, ws, budget)


@pytest.fixture
def store() -> Iterator[SqliteEventStore]:
    s = SqliteEventStore(open_db(db_path()), projectors=[project_runs_and_tasks])
    try:
        yield s
    finally:
        s.close()


def _orchestrator(store: SqliteEventStore, repo: Path, worker: FakeWorker) -> Orchestrator:
    gate = SimpleGate(repo, PASS, confirm=Prompter().confirm_merge, auto_merge=True)
    return Orchestrator(
        store,
        GitWorktreeBackend(repo),
        gate,
        {"fake": worker},
        Prompter(),
        artifact_info=artifact_info,
    )


def _spec(task: str, write_set: list[str], *blocked_by: str) -> TaskSpec:
    return TaskSpec(
        id=f"r1-{task}",
        run_id="r1",
        title=task,
        instructions=f"do {task}",
        write_set=write_set,
        blocked_by=[f"r1-{b}" for b in blocked_by],
        worker="fake",
    )


def _create(orch: Orchestrator, repo: Path, specs: list[TaskSpec], max_parallel: int = 2) -> str:
    base = git(repo, "rev-parse", "main")
    return orch.create_run("g", str(repo), base, specs, "main", {}, max_parallel=max_parallel)


# --- outbox race -----------------------------------------------------------------------------------


@pytest.mark.parametrize("scoped", [False, True], ids=["all-items", "per-task"])
async def test_concurrent_outbox_processing_starts_the_worker_once(
    store: SqliteEventStore, git_repo: Path, scoped: bool
) -> None:
    worker = SlowStartWorker()
    orch = _orchestrator(store, git_repo, worker)
    run_id = _create(orch, git_repo, [_spec("a", ["a/**"])])
    while store.get_task("r1-a").state is not S.LEASED:
        await orch.advance("r1-a")

    # Two drivers reach the same pending spawn while the first `worker.start` is still awaiting.
    if scoped:
        calls = [orch.process_outbox("r1-a"), orch.process_outbox("r1-a")]
    else:
        calls = [orch.process_outbox(), orch.process_outbox()]
    await asyncio.gather(*calls)

    assert [ctx.task.id for ctx in worker.contexts] == ["r1-a"]
    assert store.get_task("r1-a").state is S.RUNNING
    assert store.pending_outbox() == []
    assert await orch.run(run_id) == "done"


async def test_outbox_for_one_task_leaves_other_tasks_items_alone(
    store: SqliteEventStore, git_repo: Path
) -> None:
    worker = FakeWorker()
    orch = _orchestrator(store, git_repo, worker)
    run_id = _create(orch, git_repo, [_spec("a", ["a/**"]), _spec("b", ["b/**"])])
    for task in ("r1-a", "r1-b"):
        while store.get_task(task).state is not S.LEASED:
            await orch.advance(task)

    await orch.process_outbox("r1-a")

    assert store.get_task("r1-a").state is S.RUNNING
    assert store.get_task("r1-b").state is S.LEASED
    assert [item.payload["task_id"] for item in store.pending_outbox()] == ["r1-b"]
    assert await orch.run(run_id) == "done"


# --- scheduling ----------------------------------------------------------------------------------


class TimedWorker(FakeWorker):
    """FakeWorker with a delay per task and a record of how many tasks worked at once."""

    def __init__(self, delays: dict[str, float], **kw: Any) -> None:
        super().__init__(**kw)
        self.delays = delays
        self.active = 0
        self.peak = 0

    async def _work(self, spec: TaskSpec, worktree: Path) -> WorkerResult:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delays.get(spec.id.removeprefix("r1-"), 0.0))
            return await super()._work(spec, worktree)
        finally:
            self.active -= 1


class CountingGate:
    """The real gate, slowed down a little and watched for concurrent checks."""

    def __init__(self, repo: Path, explode_for: str | None = None) -> None:
        self.inner = SimpleGate(repo, PASS, confirm=Prompter().confirm_merge, auto_merge=True)
        self.explode_for = explode_for
        self.active = 0
        self.peak = 0
        self.order: list[str] = []

    async def check(
        self, diff: DiffArtifact, target_branch: str, before_merge: BeforeMerge | None = None
    ) -> GateReport:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            self.order.append(diff.task_id)
            if diff.task_id == self.explode_for:
                raise RuntimeError("gate exploded")
            await asyncio.sleep(0.2)  # widen the window in which a second check could slip in
            return await self.inner.check(diff, target_branch, before_merge)
        finally:
            self.active -= 1

    async def branch_head(self, branch: str) -> str:
        return await self.inner.branch_head(branch)


def _scheduler(
    store: SqliteEventStore, repo: Path, worker: FakeWorker, gate: CountingGate | None = None
) -> tuple[Orchestrator, CountingGate]:
    gate = gate or CountingGate(repo)
    orch = Orchestrator(
        store,
        GitWorktreeBackend(repo),
        gate,
        {"fake": worker},
        Prompter(),
        artifact_info=artifact_info,
    )
    return orch, gate


def _interval(store: SqliteEventStore, task: str) -> tuple[datetime, datetime]:
    events = store.read_stream(f"task:r1-{task}")
    start = next(e.ts for e in events if e.type == "worker.started")
    end = next(e.ts for e in events if e.type == "worker.result")
    return start, end


def _overlap(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


async def test_independent_tasks_run_at_the_same_time(
    store: SqliteEventStore, git_repo: Path
) -> None:
    worker = TimedWorker({"a": 1.0, "b": 1.0})
    orch, _ = _scheduler(store, git_repo, worker)
    run_id = _create(orch, git_repo, [_spec("a", ["a/**"]), _spec("b", ["b/**"])])

    assert await orch.run(run_id) == "done"

    a, b = _interval(store, "a"), _interval(store, "b")
    assert _overlap(a, b)
    assert (max(a[1], b[1]) - min(a[0], b[0])).total_seconds() < 1.8
    assert worker.peak == 2


@pytest.mark.parametrize(
    ("write_sets", "max_parallel"),
    [((["src/**"], ["src/a.py"]), 2), ((["a/**"], ["b/**"]), 1)],
    ids=["overlapping-write-sets", "max-parallel-1"],
)
async def test_tasks_that_may_not_share_time_never_do(
    store: SqliteEventStore,
    git_repo: Path,
    write_sets: tuple[list[str], list[str]],
    max_parallel: int,
) -> None:
    worker = TimedWorker({"a": 0.4, "b": 0.4})
    orch, _ = _scheduler(store, git_repo, worker)
    specs = [_spec("a", write_sets[0]), _spec("b", write_sets[1])]
    run_id = _create(orch, git_repo, specs, max_parallel=max_parallel)

    assert await orch.run(run_id) == "done"

    assert not _overlap(_interval(store, "a"), _interval(store, "b"))
    assert worker.peak == 1


async def test_max_parallel_caps_concurrent_workers(
    store: SqliteEventStore, git_repo: Path
) -> None:
    tasks = [f"t{i}" for i in range(5)]
    worker = TimedWorker(dict.fromkeys(tasks, 0.3))
    orch, _ = _scheduler(store, git_repo, worker)
    run_id = _create(orch, git_repo, [_spec(t, [f"{t}/**"]) for t in tasks], max_parallel=2)

    assert await orch.run(run_id) == "done"

    assert worker.peak == 2
    assert all(t.state is S.DONE for t in store.list_tasks(run_id))


async def test_gate_runs_one_task_at_a_time(store: SqliteEventStore, git_repo: Path) -> None:
    worker = TimedWorker({"a": 0.2, "b": 0.2})
    orch, gate = _scheduler(store, git_repo, worker)
    run_id = _create(orch, git_repo, [_spec("a", ["a/**"]), _spec("b", ["b/**"])])

    assert await orch.run(run_id) == "done"

    assert gate.peak == 1
    assert sorted(gate.order) == ["r1-a", "r1-b"]
    first, second = gate.order
    # The second task was rebased onto the first one's merge: a straight line on main.
    subjects = git(git_repo, "log", "--format=%s", "-2", "main").splitlines()
    assert subjects == [f"caecilia: task {second}", f"caecilia: task {first}"]
    assert git(git_repo, "rev-list", "--merges", "--count", "main") == "0"


async def test_one_permanent_failure_does_not_stop_independent_tasks(
    store: SqliteEventStore, git_repo: Path
) -> None:
    worker = TimedWorker({"a": 0.1, "b": 0.3}, fail_for={"a": "permanent"})
    orch, _ = _scheduler(store, git_repo, worker)
    specs = [_spec("a", ["a/**"]), _spec("b", ["b/**"]), _spec("c", ["c/**"], "a")]
    run_id = _create(orch, git_repo, specs)

    assert await orch.run(run_id) == "failed"

    states = {t.id: t.state for t in store.list_tasks(run_id)}
    assert states == {"r1-a": S.FAILED, "r1-b": S.DONE, "r1-c": S.CANCELLED}


async def test_dependent_task_waits_for_its_blocker(
    store: SqliteEventStore, git_repo: Path
) -> None:
    worker = TimedWorker({"a": 0.3, "b": 0.3, "c": 0.3})
    orch, _ = _scheduler(store, git_repo, worker)
    specs = [_spec("a", ["a/**"]), _spec("b", ["b/**"], "a"), _spec("c", ["c/**"])]
    run_id = _create(orch, git_repo, specs)

    assert await orch.run(run_id) == "done"

    assert _overlap(_interval(store, "a"), _interval(store, "c"))  # independent: together
    a_done = next(
        e.ts
        for e in store.read_stream("task:r1-a")
        if e.type == "task.state_changed" and e.payload["to_state"] == "DONE"
    )
    assert _interval(store, "b")[0] > a_done  # b only started after a was merged


async def test_unexpected_error_cancels_other_drivers(
    store: SqliteEventStore, git_repo: Path
) -> None:
    before = asyncio.all_tasks()
    worker = TimedWorker({"a": 0.1, "b": 30.0})
    orch, _ = _scheduler(store, git_repo, worker, CountingGate(git_repo, explode_for="r1-a"))
    run_id = _create(orch, git_repo, [_spec("a", ["a/**"]), _spec("b", ["b/**"])])

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="gate exploded"):
        await orch.run(run_id)

    assert time.monotonic() - started < 10  # b's 30 s worker was cancelled, not awaited
    await asyncio.sleep(0)
    assert asyncio.all_tasks() - before == set()
    assert worker.active == 0


async def test_tasks_found_integrating_still_take_the_merge_slot(
    store: SqliteEventStore, git_repo: Path
) -> None:
    # Bring two tasks to AWAITING_GATE, then move both to INTEGRATING as a crashed run would.
    first, _ = _scheduler(store, git_repo, FakeWorker())
    run_id = _create(first, git_repo, [_spec("a", ["a/**"]), _spec("b", ["b/**"])])
    for task in ("r1-a", "r1-b"):
        while store.get_task(task).state is not S.AWAITING_GATE:
            await first.advance(task)
        row = store.get_task(task)
        change = TaskStateChange(
            task_id=task,
            from_state=S.AWAITING_GATE,
            to_state=S.INTEGRATING,
            revision=row.revision + 1,
        )
        store.append(
            [
                NewEvent(
                    stream=f"task:{task}",
                    type="task.state_changed",
                    actor=Actor(kind="orchestrator", id="crashed"),
                    payload=change.model_dump(mode="json"),
                )
            ]
        )

    resumed, gate = _scheduler(store, git_repo, FakeWorker())
    assert await resumed.resume(run_id) == "done"
    assert gate.peak == 1
    assert len(gate.order) == 2


def test_bootstrap_serializes_questions(git_repo: Path) -> None:
    orch = build_orchestrator(git_repo, "fake", PASS, Prompter())
    try:
        assert isinstance(orch.prompter, SerializedPrompter)
    finally:
        orch.close()
