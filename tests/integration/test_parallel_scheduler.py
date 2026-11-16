"""Parallel scheduler: max_parallel, write-set conflicts, serial gate (backlog M2-03, ADR-0013)."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from caecilia_harness.bootstrap import artifact_info
from caecilia_harness.core.models import (
    Action,
    Budget,
    ContextPack,
    GateReport,
    RunHandle,
    TaskSpec,
    Workspace,
)
from caecilia_harness.core.orchestrator import Orchestrator
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
