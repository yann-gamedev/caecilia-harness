"""Projections for graph revisions: blockers_changed and run.reopened (backlog M2-06)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from caecilia_harness.core.models import Actor, NewEvent, TaskSpec, TaskStateChange
from caecilia_harness.core.states import TaskState
from caecilia_harness.paths import db_path
from caecilia_harness.store.db import open_db
from caecilia_harness.store.errors import ProjectionError
from caecilia_harness.store.events import SqliteEventStore
from caecilia_harness.store.projections import project_runs_and_tasks, rebuild_projections

ORCH = Actor(kind="orchestrator", id="o")


@pytest.fixture
def store() -> Iterator[SqliteEventStore]:
    s = SqliteEventStore(open_db(db_path()), projectors=[project_runs_and_tasks])
    run = {"run_id": "r1", "goal": "g", "repo_path": ".", "base_commit": "b"}
    events = [NewEvent(stream="run:r1", type="run.created", actor=ORCH, payload=run)]
    for task_id, blockers in (("a", []), ("b", []), ("c", ["r1-a"])):
        spec = TaskSpec(
            id=f"r1-{task_id}", run_id="r1", title=task_id, instructions="x", blocked_by=blockers
        )
        events.append(
            _ev(f"task:{spec.id}", "task.created", {"spec": spec.model_dump(mode="json")})
        )
    s.append(events)
    try:
        yield s
    finally:
        s.close()


def _ev(stream: str, type_: str, payload: dict[str, Any]) -> NewEvent:
    return NewEvent(stream=stream, type=type_, actor=ORCH, payload=payload)


def _deps(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT blocker_id FROM task_deps WHERE task_id = ? ORDER BY blocker_id", (task_id,)
    )
    return [row[0] for row in rows]


def _blockers_changed(task_id: str, blocked_by: list[str]) -> NewEvent:
    return _ev(
        f"task:{task_id}",
        "task.blockers_changed",
        {"task_id": task_id, "blocked_by": blocked_by},
    )


def test_blockers_changed_replaces_deps_and_spec(store: SqliteEventStore) -> None:
    store.append([_blockers_changed("r1-c", ["r1-b", "r1-a"])])
    assert _deps(store.connection, "r1-c") == ["r1-a", "r1-b"]
    assert store.get_task("r1-c").spec.blocked_by == ["r1-b", "r1-a"]

    store.append([_blockers_changed("r1-c", [])])
    assert _deps(store.connection, "r1-c") == []
    assert store.get_task("r1-c").spec.blocked_by == []


def test_blockers_change_only_while_pending(store: SqliteEventStore) -> None:
    change = TaskStateChange(
        task_id="r1-b", from_state=TaskState.PENDING, to_state=TaskState.READY, revision=2
    )
    store.append([_ev("task:r1-b", "task.state_changed", change.model_dump(mode="json"))])
    with pytest.raises(ProjectionError, match="only while PENDING"):
        store.append([_blockers_changed("r1-b", ["r1-a"])])
    assert store.get_task("r1-b").spec.blocked_by == []


def test_blockers_changed_for_unknown_task(store: SqliteEventStore) -> None:
    with pytest.raises(ProjectionError, match="unknown task"):
        store.append([_blockers_changed("r1-ghost", [])])


def test_run_reopened(store: SqliteEventStore) -> None:
    conn = store.connection
    store.append([_ev("run:r1", "run.finished", {"run_id": "r1", "status": "needs_human"})])
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "needs_human"
    store.append([_ev("run:r1", "run.reopened", {"run_id": "r1"})])
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "running"
    with pytest.raises(ProjectionError, match="unknown run"):
        store.append([_ev("run:r9", "run.reopened", {"run_id": "r9"})])


def test_rebuild_after_revisions_is_identical(store: SqliteEventStore) -> None:
    conn = store.connection
    store.append([_blockers_changed("r1-c", ["r1-b"])])
    store.append([_ev("run:r1", "run.finished", {"run_id": "r1", "status": "failed"})])
    store.append([_ev("run:r1", "run.reopened", {"run_id": "r1"})])
    store.append([_blockers_changed("r1-b", ["r1-a"])])

    def dump() -> dict[str, list[tuple[Any, ...]]]:
        return {
            table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]  # noqa: S608
            for table in ("runs", "tasks", "task_deps")
        }

    before = dump()
    rebuild_projections(conn)
    assert dump() == before
