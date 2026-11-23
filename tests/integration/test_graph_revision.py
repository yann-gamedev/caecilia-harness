"""Revising the graph of a stopped run, then resuming it (backlog M2-06, ADR-0003)."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from caecilia_harness.bootstrap import artifact_info
from caecilia_harness.core.models import (
    Action,
    AddTask,
    CancelTask,
    GateReport,
    GraphOp,
    RetryTask,
    SetBlockers,
    TaskDraft,
    TaskSpec,
)
from caecilia_harness.core.orchestrator import (
    GraphRevisionConflictError,
    GraphRevisionError,
    Orchestrator,
)
from caecilia_harness.core.states import TaskState
from caecilia_harness.gate.simple import SimpleGate
from caecilia_harness.paths import db_path
from caecilia_harness.store.db import open_db
from caecilia_harness.store.events import SqliteEventStore
from caecilia_harness.store.projections import project_runs_and_tasks, rebuild_projections
from caecilia_harness.workers.fake import FakeWorker
from caecilia_harness.workspace.git_worktree import GitWorktreeBackend
from tests.integration.conftest import git

S = TaskState
PASS = [sys.executable, "-c", "print('ok')"]


class Prompter:
    def __init__(self, merge: bool = True) -> None:
        self.merge = merge

    async def ask_approval(self, action: Action) -> bool:
        return False

    async def confirm_merge(self, report: GateReport) -> bool:
        return self.merge


@pytest.fixture
def store() -> Iterator[SqliteEventStore]:
    s = SqliteEventStore(open_db(db_path()), projectors=[project_runs_and_tasks])
    try:
        yield s
    finally:
        s.close()


def _orchestrator(
    store: SqliteEventStore, repo: Path, prompter: Prompter | None = None, auto_merge: bool = True
) -> Orchestrator:
    prompter = prompter or Prompter()
    gate = SimpleGate(repo, PASS, confirm=prompter.confirm_merge, auto_merge=auto_merge)
    return Orchestrator(
        store,
        GitWorktreeBackend(repo),
        gate,
        {"fake": FakeWorker()},
        prompter,
        artifact_info=artifact_info,
    )


def _spec(task: str, *blocked_by: str) -> TaskSpec:
    return TaskSpec(
        id=f"r1-{task}",
        run_id="r1",
        title=task,
        instructions=f"tulis {task}",
        write_set=[f"{task}/**"],
        blocked_by=[f"r1-{b}" for b in blocked_by],
        worker="fake",
    )


def _create(orch: Orchestrator, repo: Path, *specs: TaskSpec) -> str:
    base = git(repo, "rev-parse", "main")
    return orch.create_run("g", str(repo), base, list(specs), "main", {"worker_kind": "fake"})


def _draft(task: str) -> TaskDraft:
    return TaskDraft(id=task, title=task, instructions=f"tulis {task}", write_set=[f"{task}/**"])


def _revision(store: SqliteEventStore) -> dict[str, Any]:
    return store.read_stream("graph:r1")[-1].payload


def _count(store: SqliteEventStore) -> int:
    return len(store.read_all())


def _run_status(store: SqliteEventStore) -> str:
    return str(store.connection.execute("SELECT status FROM runs WHERE id = 'r1'").fetchone()[0])


async def test_added_task_runs_on_resume(store: SqliteEventStore, git_repo: Path) -> None:
    orch = _orchestrator(store, git_repo)
    assert await orch.run(_create(orch, git_repo, _spec("a"))) == "done"

    new_revision = orch.revise_graph("r1", [AddTask(task=_draft("b"), blocked_by=["a"])], 1)

    assert new_revision == 2
    revised = _revision(store)
    assert revised["revision"] == 2
    assert revised["tasks"] == ["r1-a", "r1-b"]
    assert revised["edges"] == [["r1-b", "r1-a"]]
    assert [op["op"] for op in revised["ops"]] == ["add_task"]
    assert [e.type for e in store.read_stream("run:r1")][-1] == "run.reopened"
    assert _run_status(store) == "running"
    added = store.get_task("r1-b")
    assert (added.state, added.spec.worker, added.spec.blocked_by) == (S.PENDING, "fake", ["r1-a"])

    assert await orch.resume("r1") == "done"
    assert store.get_task("r1-b").state is S.DONE
    assert git(git_repo, "show", "main:b/caecilia_fake_b.md") == "tulis b"


async def test_cycle_is_refused_and_nothing_is_written(
    store: SqliteEventStore, git_repo: Path
) -> None:
    orch = _orchestrator(store, git_repo)
    _create(orch, git_repo, _spec("a"), _spec("b", "a"))
    before = _count(store)

    with pytest.raises(GraphRevisionError) as info:
        orch.revise_graph("r1", [SetBlockers(task_id="a", blocked_by=["b"])], 1)

    assert any("cycle" in problem and "r1-a" in problem for problem in info.value.problems)
    assert _count(store) == before
    assert store.get_task("r1-a").spec.blocked_by == []


async def test_blockers_of_a_started_task_cannot_change(
    store: SqliteEventStore, git_repo: Path
) -> None:
    orch = _orchestrator(store, git_repo)
    run_id = _create(orch, git_repo, _spec("a"), _spec("b"))
    await orch.advance("r1-a")  # PENDING -> READY
    while store.get_task("r1-b").state is not S.RUNNING:
        await orch.advance("r1-b")

    for task, state in (("a", "READY"), ("b", "RUNNING")):
        with pytest.raises(GraphRevisionError) as info:
            orch.revise_graph("r1", [SetBlockers(task_id=task, blocked_by=[])], 1)
        [problem] = info.value.problems
        assert state in problem and "PENDING" in problem

    assert await orch.run(run_id) == "done"


async def test_stale_expected_revision_is_a_conflict(
    store: SqliteEventStore, git_repo: Path
) -> None:
    orch = _orchestrator(store, git_repo)
    _create(orch, git_repo, _spec("a"), _spec("b"))
    orch.revise_graph("r1", [SetBlockers(task_id="b", blocked_by=["a"])], 1)
    before = _count(store)

    with pytest.raises(GraphRevisionConflictError) as info:
        orch.revise_graph("r1", [SetBlockers(task_id="b", blocked_by=[])], 1)

    assert (info.value.expected, info.value.actual) == (1, 2)
    assert _count(store) == before


async def test_retry_needs_human_task_and_finish_it(
    store: SqliteEventStore, git_repo: Path
) -> None:
    prompter = Prompter(merge=False)
    orch = _orchestrator(store, git_repo, prompter, auto_merge=False)
    assert await orch.run(_create(orch, git_repo, _spec("a"))) == "needs_human"

    assert orch.revise_graph("r1", [RetryTask(task_id="a")], 1) == 2

    task = store.get_task("r1-a")
    assert task.state is S.READY
    change = [e for e in store.read_stream("task:r1-a") if e.type == "task.state_changed"][-1]
    assert change.payload["reason"] == "dibereskan manusia"
    assert _run_status(store) == "running"

    prompter.merge = True
    assert await orch.resume("r1") == "done"
    assert git(git_repo, "show", "main:a/caecilia_fake_a.md") == "tulis a"


async def test_retry_only_applies_to_needs_human(store: SqliteEventStore, git_repo: Path) -> None:
    orch = _orchestrator(store, git_repo)
    _create(orch, git_repo, _spec("a"))
    with pytest.raises(GraphRevisionError, match="NEEDS_HUMAN"):
        orch.revise_graph("r1", [RetryTask(task_id="a")], 1)


async def test_cancelled_blocker_cancels_its_dependent_on_resume(
    store: SqliteEventStore, git_repo: Path
) -> None:
    orch = _orchestrator(store, git_repo, Prompter(merge=False), auto_merge=False)
    assert await orch.run(_create(orch, git_repo, _spec("a"), _spec("b", "a"))) == "needs_human"
    assert store.get_task("r1-b").state is S.PENDING

    orch.revise_graph("r1", [CancelTask(task_id="a")], 1)
    assert store.get_task("r1-a").state is S.CANCELLED

    assert await orch.resume("r1") == "cancelled"
    b = [e for e in store.read_stream("task:r1-b") if e.type == "task.state_changed"][-1]
    assert (b.payload["to_state"], b.payload["reason"]) == ("CANCELLED", "blocker r1-a CANCELLED")


@pytest.mark.parametrize("state", ["RUNNING", "DONE"])
async def test_cancel_refuses_running_and_finished_tasks(
    store: SqliteEventStore, git_repo: Path, state: str
) -> None:
    orch = _orchestrator(store, git_repo)
    run_id = _create(orch, git_repo, _spec("a"))
    while store.get_task("r1-a").state.value != state:
        await orch.advance("r1-a")

    with pytest.raises(GraphRevisionError, match=state):
        orch.revise_graph("r1", [CancelTask(task_id="a")], 1)
    await orch.run(run_id)


async def test_one_bad_operation_rejects_the_whole_revision(
    store: SqliteEventStore, git_repo: Path
) -> None:
    orch = _orchestrator(store, git_repo)
    _create(orch, git_repo, _spec("a"))
    before = _count(store)
    ops: list[GraphOp] = [
        AddTask(task=_draft("b")),
        SetBlockers(task_id="ghost", blocked_by=[]),
        AddTask(task=_draft("a")),  # duplicate id
        AddTask(task=_draft("c"), blocked_by=["nope"]),
    ]

    with pytest.raises(GraphRevisionError) as info:
        orch.revise_graph("r1", ops, 1)

    joined = "\n".join(info.value.problems)
    assert "ghost" in joined
    assert "duplicate" in joined or "already exists" in joined
    assert "missing_blocker" in joined and "nope" in joined
    assert _count(store) == before
    assert [t.id for t in store.list_tasks("r1")] == ["r1-a"]


async def test_several_operations_land_in_one_revision(
    store: SqliteEventStore, git_repo: Path
) -> None:
    orch = _orchestrator(store, git_repo)
    _create(orch, git_repo, _spec("a"), _spec("b"))
    ops: list[GraphOp] = [
        AddTask(task=_draft("c")),
        AddTask(task=_draft("d"), blocked_by=["c"]),  # blocked by a task added in the same revision
        SetBlockers(task_id="b", blocked_by=["a", "c"]),
        CancelTask(task_id="a"),
    ]
    assert orch.revise_graph("r1", ops, 1) == 2
    assert [t.id for t in store.list_tasks("r1")] == ["r1-a", "r1-b", "r1-c", "r1-d"]
    assert store.get_task("r1-b").spec.blocked_by == ["r1-a", "r1-c"]
    assert store.get_task("r1-a").state is S.CANCELLED
    assert len(_revision(store)["ops"]) == 4
    # The run had not finished yet: no reopening needed.
    assert "run.reopened" not in [e.type for e in store.read_stream("run:r1")]


async def test_rebuild_after_several_revisions_is_identical(
    store: SqliteEventStore, git_repo: Path
) -> None:
    orch = _orchestrator(store, git_repo)
    assert await orch.run(_create(orch, git_repo, _spec("a"))) == "done"
    orch.revise_graph("r1", [AddTask(task=_draft("b"), blocked_by=["a"])], 1)
    orch.revise_graph("r1", [AddTask(task=_draft("c")), SetBlockers(task_id="b", blocked_by=[])], 2)
    orch.revise_graph("r1", [CancelTask(task_id="c")], 3)

    conn = store.connection
    tables = ("runs", "tasks", "task_deps")
    before = {
        t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")] for t in tables
    }  # noqa: S608
    rebuild_projections(conn)
    after = {
        t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")] for t in tables
    }  # noqa: S608
    assert after == before
    assert _revision(store)["revision"] == 4
