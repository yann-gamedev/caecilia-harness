"""Run token budgets and pre-gate write-set enforcement (backlog M2-04)."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from caecilia_harness.bootstrap import artifact_info
from caecilia_harness.core.models import (
    Actor,
    Budget,
    DiffArtifact,
    GateReport,
    NewEvent,
    RunHandle,
    TaskSpec,
    WorkerEvent,
)
from caecilia_harness.core.orchestrator import Orchestrator
from caecilia_harness.core.ports import BeforeMerge
from caecilia_harness.core.states import TaskState as S
from caecilia_harness.gate.simple import SimpleGate
from caecilia_harness.paths import db_path
from caecilia_harness.store.db import open_db
from caecilia_harness.store.events import SqliteEventStore
from caecilia_harness.store.projections import project_runs_and_tasks
from caecilia_harness.workers.fake import FakeWorker
from caecilia_harness.workspace.git_worktree import GitWorktreeBackend
from tests.integration.conftest import git
from tests.integration.test_orchestrator import FakePrompter


@pytest.fixture
def store() -> Iterator[SqliteEventStore]:
    store = SqliteEventStore(open_db(db_path()), projectors=[project_runs_and_tasks])
    try:
        yield store
    finally:
        store.close()


class UsageWorker(FakeWorker):
    async def events(self, handle: RunHandle) -> AsyncIterator[WorkerEvent]:
        async for event in super().events(handle):
            yield event
        yield WorkerEvent(kind="usage", data={"input_tokens": 4, "output_tokens": 3}, durable=True)


class CountingGate(SimpleGate):
    def __init__(self, repo: Path) -> None:
        super().__init__(
            repo,
            [sys.executable, "-c", "print('ok')"],
            confirm=FakePrompter().confirm_merge,
            auto_merge=True,
        )
        self.calls: list[str] = []

    async def check(
        self, diff: DiffArtifact, target_branch: str, before_merge: BeforeMerge | None = None
    ) -> GateReport:
        self.calls.append(diff.task_id)
        return await super().check(diff, target_branch, before_merge)


def make_orchestrator(
    store: SqliteEventStore, repo: Path, worker: FakeWorker
) -> tuple[Orchestrator, CountingGate]:
    gate = CountingGate(repo)
    return Orchestrator(
        store,
        GitWorktreeBackend(repo),
        gate,
        {"fake": worker},
        FakePrompter(),
        artifact_info=artifact_info,
    ), gate


def spec(
    task: str, patterns: list[str] | None = None, blockers: list[str] | None = None
) -> TaskSpec:
    return TaskSpec(
        id=f"r-{task}",
        run_id="r",
        title=task,
        instructions=task,
        worker="fake",
        write_set=patterns if patterns is not None else [f"{task}/**"],
        blocked_by=blockers or [],
    )


@pytest.mark.parametrize("budget", [Budget(max_output_tokens=2), Budget(max_input_tokens=3)])
async def test_budget_stops_new_leases_and_resume(
    store: SqliteEventStore, git_repo: Path, budget: Budget
) -> None:
    worker = UsageWorker()
    orch, gate = make_orchestrator(store, git_repo, worker)
    tasks = [spec("a"), spec("b"), spec("c", blockers=["r-b"])]
    run_id = orch.create_run(
        "g", str(git_repo), git(git_repo, "rev-parse", "main"), tasks, max_parallel=1, budget=budget
    )
    assert await orch.run(run_id) == "needs_human"
    assert gate.calls == ["r-a"]
    assert [store.get_task(t.id).state for t in tasks] == [S.DONE, S.READY, S.PENDING]
    events = store.read_all()
    [exceeded] = [e for e in events if e.type == "run.budget_exceeded"]
    assert exceeded.payload["used"] == {"input_tokens": 4, "output_tokens": 3}
    assert exceeded.payload["limit"]["max_output_tokens"] == budget.max_output_tokens
    assert not any(e.type == "task.lease_acquired" and e.seq > exceeded.seq for e in events)
    finished = [e for e in events if e.type == "run.finished"][-1]
    assert "budget" in finished.payload["reason"]

    resumed, _ = make_orchestrator(store, git_repo, UsageWorker())
    assert await resumed.resume(run_id) == "needs_human"
    assert len([e for e in store.read_all() if e.type == "run.budget_exceeded"]) == 1
    assert len([e for e in store.read_all() if e.type == "task.lease_acquired"]) == 1


async def test_budget_sums_all_task_streams_and_allows_exact_limit(
    store: SqliteEventStore, git_repo: Path
) -> None:
    orch, _ = make_orchestrator(store, git_repo, UsageWorker())
    tasks = [spec(t) for t in ("a", "b", "c")]
    run_id = orch.create_run(
        "g",
        str(git_repo),
        git(git_repo, "rev-parse", "main"),
        tasks,
        max_parallel=1,
        budget=Budget(max_output_tokens=3),
    )
    assert await orch.run(run_id) == "needs_human"
    assert [store.get_task(t.id).state for t in tasks] == [S.DONE, S.DONE, S.READY]
    [exceeded] = [e for e in store.read_stream("run:r") if e.type == "run.budget_exceeded"]
    assert exceeded.payload["used"]["output_tokens"] == 6


async def test_budget_counts_failed_attempt_usage_before_retry(
    store: SqliteEventStore, git_repo: Path
) -> None:
    worker = UsageWorker(fail_with="transient provider failure")
    orch, gate = make_orchestrator(store, git_repo, worker)
    orch.create_run(
        "g",
        str(git_repo),
        git(git_repo, "rev-parse", "main"),
        [spec("a")],
        budget=Budget(max_output_tokens=2),
    )
    assert await orch.run("r") == "needs_human"
    assert store.get_task("r-a").state is S.READY
    assert len(worker.contexts) == 1
    assert gate.calls == []


@pytest.mark.parametrize("after_run", [False, True])
async def test_advance_cannot_lease_after_budget_exhaustion(
    store: SqliteEventStore, git_repo: Path, after_run: bool
) -> None:
    orch, _ = make_orchestrator(store, git_repo, FakeWorker())
    orch.create_run(
        "g",
        str(git_repo),
        git(git_repo, "rev-parse", "main"),
        [spec("a")],
        budget=Budget(max_output_tokens=0),
    )
    await orch.advance("r-a")
    store.append(
        [
            NewEvent(
                stream="task:r-a",
                type="usage.recorded",
                actor=Actor(kind="worker", id="old"),
                payload={"input_tokens": 0, "output_tokens": 1},
            )
        ]
    )
    fresh, _ = make_orchestrator(store, git_repo, FakeWorker())
    if after_run:
        assert await fresh.run("r") == "needs_human"
    await fresh.advance("r-a")
    await fresh.advance("r-a")
    assert store.get_task("r-a").state is S.READY
    assert not any(e.type == "task.lease_acquired" for e in store.read_all())
    assert len([e for e in store.read_all() if e.type == "run.budget_exceeded"]) == 1


async def test_rename_from_outside_write_set_never_reaches_gate(
    store: SqliteEventStore, git_repo: Path
) -> None:
    (git_repo / "web").mkdir()
    (git_repo / "web" / "x.txt").write_text("unchanged content", encoding="utf-8")
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-m", "seed rename source")
    worker = FakeWorker()
    orch, gate = make_orchestrator(store, git_repo, worker)
    orch.create_run("g", str(git_repo), git(git_repo, "rev-parse", "main"), [spec("a", ["api/**"])])
    while store.get_task("r-a").state is not S.RUNNING:
        await orch.advance("r-a")
    # Simulate worker moving an unchanged tracked file into its allowed directory.
    live = orch._live["r-a"]
    await worker.result(live.handle)
    worktree = Path(orch._workspaces["r-a"].path)
    (worktree / "api").mkdir(exist_ok=True)
    (worktree / "web" / "x.txt").rename(worktree / "api" / "x.txt")
    await orch.advance("r-a")
    await orch.advance("r-a")
    assert gate.calls == []
    assert store.get_task("r-a").state is S.REWORK
    [failed] = [e for e in store.read_stream("task:r-a") if e.type == "gate.failed"]
    assert failed.payload["report"]["checks"] == {"write_set": False}
    assert "web/x.txt" in orch._handoff_notes("r-a")[-1]


async def test_non_token_run_budget_fields_are_not_enforced(
    store: SqliteEventStore, git_repo: Path
) -> None:
    orch, _ = make_orchestrator(store, git_repo, UsageWorker())
    orch.create_run(
        "g",
        str(git_repo),
        git(git_repo, "rev-parse", "main"),
        [spec("a")],
        budget=Budget(max_cost_usd=0, max_steps=0, max_wall_seconds=0),
    )
    assert await orch.run("r") == "done"
    assert not any(e.type == "run.budget_exceeded" for e in store.read_all())


async def test_resume_rebuilds_usage_even_before_budget_event_was_written(
    store: SqliteEventStore, git_repo: Path
) -> None:
    orch, _ = make_orchestrator(store, git_repo, FakeWorker())
    run_id = orch.create_run(
        "g",
        str(git_repo),
        git(git_repo, "rev-parse", "main"),
        [spec("a")],
        budget=Budget(max_output_tokens=0),
    )
    # Crash after usage was durable but before the budget event was appended.
    store.append(
        [
            NewEvent(
                stream="task:r-a",
                type="usage.recorded",
                actor=Actor(kind="worker", id="old"),
                payload={"input_tokens": 0, "output_tokens": 1},
            )
        ]
    )
    resumed, _ = make_orchestrator(store, git_repo, FakeWorker())
    assert await resumed.resume(run_id) == "needs_human"
    assert store.get_task("r-a").state is S.READY
    assert not any(e.type == "task.lease_acquired" for e in store.read_all())
    assert len([e for e in store.read_all() if e.type == "run.budget_exceeded"]) == 1


async def test_budget_finishes_already_running_tasks(
    store: SqliteEventStore, git_repo: Path
) -> None:
    release = asyncio.Event()
    b_started = asyncio.Event()

    class Worker(UsageWorker):
        async def events(self, handle: RunHandle) -> AsyncIterator[WorkerEvent]:
            if handle.task_id == "r-a":
                await b_started.wait()
            if handle.task_id == "r-b":
                b_started.set()
                await release.wait()
            async for event in super().events(handle):
                yield event
            if handle.task_id == "r-a":
                release.set()

    orch, gate = make_orchestrator(store, git_repo, Worker())
    run_id = orch.create_run(
        "g",
        str(git_repo),
        git(git_repo, "rev-parse", "main"),
        [spec(t) for t in ("a", "b", "c")],
        max_parallel=2,
        budget=Budget(max_output_tokens=2),
    )
    assert await asyncio.wait_for(orch.run(run_id), 30) == "needs_human"
    assert sorted(gate.calls) == ["r-a", "r-b"]
    assert store.get_task("r-c").state is S.READY


@pytest.mark.parametrize("patterns", [["api/**"], ["api/*"], ["api/?.txt"]])
async def test_outside_write_set_skips_gate_and_supplies_handoff(
    store: SqliteEventStore, git_repo: Path, patterns: list[str]
) -> None:
    worker = FakeWorker(files={"web/x.txt": "bad"})
    orch, gate = make_orchestrator(store, git_repo, worker)
    task = spec("a", patterns)
    orch.create_run("g", str(git_repo), git(git_repo, "rev-parse", "main"), [task])
    while store.get_task(task.id).state is not S.AWAITING_GATE:
        await orch.advance(task.id)
    await orch.advance(task.id)
    assert store.get_task(task.id).state is S.REWORK
    assert gate.calls == []
    [failed] = [e for e in store.read_stream(f"task:{task.id}") if e.type == "gate.failed"]
    assert failed.payload["report"]["checks"] == {"write_set": False}
    assert "web/x.txt" in failed.payload["report"]["output_tail"]
    await orch.advance(task.id)
    await orch.advance(task.id)
    await orch.advance(task.id)
    assert "web/x.txt" in worker.contexts[-1].handoff_notes[-1]
    assert patterns[0] in worker.contexts[-1].handoff_notes[-1]
    assert await orch.run("r") == "needs_human"
    assert gate.calls == []


@pytest.mark.parametrize("patterns", [["api/**"], [], ["**"], ["api/?.txt"]])
async def test_inside_write_set_reaches_gate(
    store: SqliteEventStore, git_repo: Path, patterns: list[str]
) -> None:
    orch, gate = make_orchestrator(store, git_repo, FakeWorker(files={"api/x.txt": "ok"}))
    orch.create_run("g", str(git_repo), git(git_repo, "rev-parse", "main"), [spec("a", patterns)])
    assert await orch.run("r") == "done"
    assert gate.calls == ["r-a"]
