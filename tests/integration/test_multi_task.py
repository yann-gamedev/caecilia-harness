"""Runs with several dependent tasks, executed serially (backlog M2-02, ADR-0013)."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from caecilia_harness.bootstrap import artifact_info, get_run_status
from caecilia_harness.core.graph import GraphError
from caecilia_harness.core.models import Action, GateReport, TaskSpec
from caecilia_harness.core.orchestrator import Orchestrator
from caecilia_harness.core.states import TaskState
from caecilia_harness.gate.simple import SimpleGate
from caecilia_harness.interfaces.cli import app
from caecilia_harness.paths import db_path
from caecilia_harness.planning.plan import Plan, load_plan, parse_plan, plan_to_specs, run_budget
from caecilia_harness.store.db import open_db
from caecilia_harness.store.events import SqliteEventStore
from caecilia_harness.store.projections import project_runs_and_tasks
from caecilia_harness.workers.fake import FakeWorker
from caecilia_harness.workspace.git_worktree import GitWorktreeBackend
from tests.integration.conftest import git

S = TaskState
PY = sys.executable
PASS = [PY, "-c", "print('ok')"]
PLANS = Path(__file__).resolve().parents[1] / "fixtures" / "plans"
runner = CliRunner()


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
    store: SqliteEventStore,
    repo: Path,
    worker: FakeWorker,
    prompter: Prompter | None = None,
    auto_merge: bool = True,
) -> Orchestrator:
    prompter = prompter or Prompter()
    gate = SimpleGate(repo, PASS, confirm=prompter.confirm_merge, auto_merge=auto_merge)
    return Orchestrator(
        store,
        GitWorktreeBackend(repo),
        gate,
        {"fake": worker},
        prompter,
        artifact_info=artifact_info,
    )


def _create(orch: Orchestrator, repo: Path, plan: Plan) -> str:
    specs = plan_to_specs(plan, "r1", default_worker="fake")
    return orch.create_run(
        plan.goal,
        str(repo),
        git(repo, "rev-parse", "main"),
        specs,
        "main",
        {},
        max_parallel=plan.max_parallel,
        budget=run_budget(plan),
    )


def _events(store: SqliteEventStore, task: str, type_: str) -> list[dict[str, object]]:
    return [e.payload for e in store.read_stream(f"task:r1-{task}") if e.type == type_]


def _task_commits(repo: Path) -> list[str]:
    return [
        s for s in git(repo, "log", "--format=%s", "main").splitlines() if s.startswith("caecilia")
    ]


async def test_dependent_task_starts_from_its_blockers_merge(
    store: SqliteEventStore, git_repo: Path
) -> None:
    orch = _orchestrator(store, git_repo, FakeWorker())
    run_id = _create(orch, git_repo, load_plan(PLANS / "chain.toml"))

    assert await orch.run(run_id) == "done"

    [api_passed] = _events(store, "api", "gate.passed")
    merged_api = api_passed["report"]["merged_commit"]  # type: ignore[index]
    [web_started] = _events(store, "web", "worker.started")
    assert web_started["base_commit"] == merged_api
    # The web worktree already contained the api change: it is in web's own commit tree.
    [web_result] = _events(store, "web", "worker.result")
    web_head = web_result["diff"]["head_commit"]  # type: ignore[index]
    tree = git(git_repo, "ls-tree", "-r", "--name-only", web_head).splitlines()
    assert "api/caecilia_fake_api.md" in tree
    assert "web/caecilia_fake_web.md" in tree

    assert git(git_repo, "show", "main:api/caecilia_fake_api.md") == "Tulis endpoint."
    assert len(_task_commits(git_repo)) == 2  # one commit per task


async def test_independent_tasks_run_in_plan_order(store: SqliteEventStore, git_repo: Path) -> None:
    orch = _orchestrator(store, git_repo, FakeWorker())
    assert await orch.run(_create(orch, git_repo, load_plan(PLANS / "independent.toml"))) == "done"

    started = [e.stream for e in store.read_all() if e.type == "worker.started"]
    assert started == ["task:r1-zeta", "task:r1-alpha"]
    assert git(git_repo, "show", "main:docs/notes.md") == "Tulis docs."


async def test_failed_blocker_cancels_its_dependents(
    store: SqliteEventStore, git_repo: Path
) -> None:
    plan = parse_plan(
        'version = 1\ngoal = "rantai"\n'
        '[[task]]\nid = "a"\ntitle = "a"\ninstructions = "a"\nwrite_set = ["a/**"]\n'
        '[[task]]\nid = "b"\ntitle = "b"\ninstructions = "b"\nwrite_set = ["b/**"]\n'
        'blocked_by = ["a"]\n'
        '[[task]]\nid = "c"\ntitle = "c"\ninstructions = "c"\nwrite_set = ["c/**"]\n'
        'blocked_by = ["b"]\n'
        '[[task]]\nid = "d"\ntitle = "d"\ninstructions = "d"\nwrite_set = ["d/**"]\n'
    )
    worker = FakeWorker(fail_for={"a": "permanent"})
    orch = _orchestrator(store, git_repo, worker)

    assert await orch.run(_create(orch, git_repo, plan)) == "failed"

    states = {t.id: t.state for t in store.list_tasks("r1")}
    assert states == {"r1-a": S.FAILED, "r1-b": S.CANCELLED, "r1-c": S.CANCELLED, "r1-d": S.DONE}
    [b_cancel] = [
        p for p in _events(store, "b", "task.state_changed") if p["to_state"] == "CANCELLED"
    ]
    assert b_cancel["reason"] == "blocker r1-a FAILED"
    [c_cancel] = [
        p for p in _events(store, "c", "task.state_changed") if p["to_state"] == "CANCELLED"
    ]
    assert c_cancel["reason"] == "blocker r1-b CANCELLED"
    assert [ctx.task.id for ctx in worker.contexts] == ["r1-a", "r1-d"]  # b and c never spawned


async def test_needs_human_blocker_keeps_dependents_pending(
    store: SqliteEventStore, git_repo: Path
) -> None:
    worker = FakeWorker()
    orch = _orchestrator(store, git_repo, worker, Prompter(merge=False), auto_merge=False)

    assert await orch.run(_create(orch, git_repo, load_plan(PLANS / "chain.toml"))) == "needs_human"

    assert store.get_task("r1-api").state is S.NEEDS_HUMAN
    assert store.get_task("r1-web").state is S.PENDING
    assert [ctx.task.id for ctx in worker.contexts] == ["r1-api"]


async def test_graph_revision_one_is_recorded(store: SqliteEventStore, git_repo: Path) -> None:
    plan = load_plan(PLANS / "chain.toml")
    orch = _orchestrator(store, git_repo, FakeWorker())
    _create(orch, git_repo, plan.model_copy(update={"max_parallel": 3}))

    [revised] = store.read_stream("graph:r1")
    assert revised.type == "graph.revised"
    assert revised.payload == {
        "revision": 1,
        "tasks": ["r1-api", "r1-web"],
        "edges": [["r1-web", "r1-api"]],
    }
    [created] = store.read_stream("run:r1")
    assert created.payload["max_parallel"] == 3
    assert created.payload["budget"] == {}


async def test_tasks_are_created_in_dependency_order(
    store: SqliteEventStore, git_repo: Path
) -> None:
    # The dependent comes first in the file; the blocker must still be created first.
    plan = parse_plan(
        'version = 1\ngoal = "terbalik"\n'
        '[[task]]\nid = "web"\ntitle = "w"\ninstructions = "w"\nwrite_set = ["web/**"]\n'
        'blocked_by = ["api"]\n'
        '[[task]]\nid = "api"\ntitle = "a"\ninstructions = "a"\nwrite_set = ["api/**"]\n'
    )
    orch = _orchestrator(store, git_repo, FakeWorker())
    assert await orch.run(_create(orch, git_repo, plan)) == "done"
    assert [t.id for t in store.list_tasks("r1")] == ["r1-api", "r1-web"]


async def test_invalid_graph_is_refused_before_anything_is_written(
    store: SqliteEventStore, git_repo: Path
) -> None:
    specs = [
        TaskSpec(id="r1-a", run_id="r1", title="a", instructions="a", blocked_by=["r1-b"]),
        TaskSpec(id="r1-b", run_id="r1", title="b", instructions="b", blocked_by=["r1-a"]),
    ]
    orch = _orchestrator(store, git_repo, FakeWorker())
    with pytest.raises(GraphError) as info:
        orch.create_run("g", str(git_repo), "abc", specs, "main", {})
    assert [issue.kind for issue in info.value.issues] == ["cycle"]
    assert store.read_all() == []


async def test_all_tasks_must_belong_to_one_run(store: SqliteEventStore, git_repo: Path) -> None:
    specs = [
        TaskSpec(id="r1-a", run_id="r1", title="a", instructions="a"),
        TaskSpec(id="r2-b", run_id="r2", title="b", instructions="b"),
    ]
    orch = _orchestrator(store, git_repo, FakeWorker())
    with pytest.raises(ValueError, match="run"):
        orch.create_run("g", str(git_repo), "abc", specs, "main", {})
    with pytest.raises(ValueError, match="at least one"):
        orch.create_run("g", str(git_repo), "abc", [], "main", {})


# --- CLI ------------------------------------------------------------------------------------------


def _check() -> str:
    return f'"{PY}" -c "print(1)"'


def test_cli_run_plan(git_repo: Path) -> None:
    args = ["run", "--plan", str(PLANS / "chain.toml"), "--repo", str(git_repo)]
    result = runner.invoke(
        app, [*args, "--check", _check(), "--worker", "fake", "--auto-merge", "--plain"]
    )
    assert result.exit_code == 0, result.output
    assert "graph.revised" in result.output
    assert (
        git(git_repo, "show", "main:web/caecilia_fake_web.md")
        == "Tulis halaman yang memakai endpoint."
    )
    assert len(_task_commits(git_repo)) == 2

    status = get_run_status()
    assert status is not None
    assert [(t.plan_id, t.blocked_by, t.state) for t in status.tasks] == [
        ("api", [], "DONE"),
        ("web", ["api"], "DONE"),
    ]
    shown = runner.invoke(app, ["status", "--plain"])
    assert "BLOCKED_BY" in shown.output
    web_line = next(line for line in shown.output.splitlines() if "-web " in line)
    assert " api " in web_line


@pytest.mark.parametrize(
    "goal_and_plan",
    [["goal", "--plan", str(PLANS / "chain.toml")], []],
    ids=["both", "neither"],
)
def test_cli_run_needs_exactly_one_of_goal_or_plan(
    git_repo: Path, goal_and_plan: list[str]
) -> None:
    result = runner.invoke(
        app,
        ["run", *goal_and_plan, "--repo", str(git_repo), "--check", _check(), "--worker", "fake"],
    )
    assert result.exit_code == 1
    assert "GOAL" in result.output and "--plan" in result.output


def test_cli_run_with_broken_plan(git_repo: Path) -> None:
    args = ["run", "--plan", str(PLANS / "broken_graph.toml"), "--repo", str(git_repo)]
    result = runner.invoke(app, [*args, "--check", _check(), "--worker", "fake"])
    assert result.exit_code == 1
    assert "cycle" in result.output


def test_cli_plan_validate() -> None:
    bad = runner.invoke(app, ["plan", "validate", str(PLANS / "broken_graph.toml")])
    assert bad.exit_code == 1
    for expected in ("cycle", "missing_blocker", "duplicate_id", "bad_pattern"):
        assert expected in bad.output

    good = runner.invoke(app, ["plan", "validate", str(PLANS / "chain.toml")])
    assert good.exit_code == 0, good.output
    assert "2 task" in good.output
