"""Revising the graph of a stopped run, then resuming it (backlog M2-06, ADR-0003)."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from caecilia_harness.bootstrap import artifact_info, get_run_status, list_runs
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
from caecilia_harness.interfaces.cli import app
from caecilia_harness.paths import db_path, harness_home
from caecilia_harness.store.db import open_db
from caecilia_harness.store.events import SqliteEventStore
from caecilia_harness.store.lock import WriterLock
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

    before = _projection(store)
    rebuild_projections(store.connection)
    assert _projection(store) == before
    assert _revision(store)["revision"] == 4


def _projection(store: SqliteEventStore) -> dict[str, list[tuple[Any, ...]]]:
    dump: dict[str, list[tuple[Any, ...]]] = {}
    for table in ("runs", "tasks", "task_deps"):
        query = f"SELECT * FROM {table} ORDER BY 1, 2"  # noqa: S608 - fixed table names
        dump[table] = [tuple(row) for row in store.connection.execute(query)]
    return dump


# --- CLI ---------------------------------------------------------------------------------------

runner = CliRunner()


def _cli_run(repo: Path, *extra: str) -> str:
    """Run one fake task through the CLI and return the run id."""
    check = f'"{sys.executable}" -c "print(1)"'
    args = ["run", "catatan", "--repo", str(repo), "--check", check, "--worker", "fake", "--plain"]
    runner.invoke(app, [*args, *extra])
    [run] = list_runs()
    return run.id


def _task_file(tmp_path: Path, task: str = "docs") -> Path:
    path = tmp_path / "task.toml"
    path.write_text(
        f'[[task]]\nid = "{task}"\ntitle = "Docs"\ninstructions = "tulis docs"\n'
        f'write_set = ["{task}/**"]\n',
        encoding="utf-8",
    )
    return path


def test_cli_show_add_and_resume(git_repo: Path, tmp_path: Path) -> None:
    run_id = _cli_run(git_repo, "--auto-merge")

    shown = runner.invoke(app, ["graph", "show", "--plain"])
    assert shown.exit_code == 0, shown.output
    assert f"Run {run_id}  revisi graf 1" in shown.output
    assert any(line.startswith("main ") and "DONE" in line for line in shown.output.splitlines())

    added = runner.invoke(
        app,
        [
            "graph",
            "add",
            "--run",
            run_id,
            "--file",
            str(_task_file(tmp_path)),
            "--blocked-by",
            "main",
        ],
    )
    assert added.exit_code == 0, added.output
    assert "Revisi graf sekarang: 2" in added.output

    shown = runner.invoke(app, ["graph", "show", "--run", run_id, "--plain"])
    docs = next(line for line in shown.output.splitlines() if line.startswith("docs "))
    assert "PENDING" in docs and " main " in docs and "docs/**" in docs

    resumed = runner.invoke(app, ["resume", "--plain"])
    assert resumed.exit_code == 0, resumed.output
    assert git(git_repo, "show", "main:docs/caecilia_fake_docs.md") == "tulis docs"


def test_cli_refusals_are_explained(git_repo: Path, tmp_path: Path) -> None:
    run_id = _cli_run(git_repo, "--auto-merge")

    deps = runner.invoke(app, ["graph", "deps", "--run", run_id, "main", "--blocked-by", ""])
    assert deps.exit_code == 1
    assert "DONE" in deps.output and "PENDING" in deps.output

    stale = runner.invoke(
        app, ["graph", "cancel", "--run", run_id, "main", "--expect-revision", "7"]
    )
    assert stale.exit_code == 1
    assert "not 7" in stale.output

    bad_file = tmp_path / "two.toml"
    bad_file.write_text('[[task]]\nid = "a"\n[[task]]\nid = "b"\n', encoding="utf-8")
    two = runner.invoke(app, ["graph", "add", "--run", run_id, "--file", str(bad_file)])
    assert two.exit_code == 1 and "tepat satu [[task]]" in two.output


def test_cli_cancel_and_retry_a_needs_human_task(git_repo: Path) -> None:
    run_id = _cli_run(git_repo)  # no TTY, no --auto-merge: the merge is declined
    assert get_run_status(run_id).tasks[0].state == "NEEDS_HUMAN"  # type: ignore[union-attr]

    retried = runner.invoke(app, ["graph", "retry", "--run", run_id, "main"])
    assert retried.exit_code == 0, retried.output
    assert get_run_status(run_id).tasks[0].state == "READY"  # type: ignore[union-attr]

    cancelled = runner.invoke(app, ["graph", "cancel", "--run", run_id, "main"])
    assert cancelled.exit_code == 0, cancelled.output
    assert "Revisi graf sekarang: 3" in cancelled.output
    assert get_run_status(run_id).tasks[0].state == "CANCELLED"  # type: ignore[union-attr]


def test_cli_writes_wait_for_a_running_run_but_show_does_not(
    git_repo: Path, tmp_path: Path
) -> None:
    run_id = _cli_run(git_repo, "--auto-merge")
    with WriterLock(harness_home() / "harness.lock"):  # as if `caecilia run` were executing
        added = runner.invoke(
            app, ["graph", "add", "--run", run_id, "--file", str(_task_file(tmp_path))]
        )
        shown = runner.invoke(app, ["graph", "show", "--run", run_id, "--plain"])
    assert added.exit_code == 1
    assert "sedang dieksekusi" in added.output
    assert shown.exit_code == 0 and "revisi graf 1" in shown.output
