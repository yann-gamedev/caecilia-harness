"""Run/task projections and enforcement of the task state table (backlog M1-03)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from caecilia_harness.core.models import Actor, NewEvent, TaskSpec, TaskStateChange
from caecilia_harness.core.states import TERMINAL, TaskState, can_transition
from caecilia_harness.paths import db_path
from caecilia_harness.store.db import apply_migrations, open_db
from caecilia_harness.store.errors import IllegalTransitionError, StaleTaskStateError
from caecilia_harness.store.events import SqliteEventStore
from caecilia_harness.store.projections import project_runs_and_tasks, rebuild_projections

S = TaskState
ORCH = Actor(kind="orchestrator", id="orch-1")
HAPPY_PATH = [S.READY, S.LEASED, S.RUNNING, S.AWAITING_GATE, S.INTEGRATING, S.DONE]


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = open_db(db_path())
    try:
        yield connection
    finally:
        connection.close()


def _store(conn: sqlite3.Connection) -> SqliteEventStore:
    return SqliteEventStore(conn, projectors=[project_runs_and_tasks])


def _run_created(run_id: str = "r1") -> NewEvent:
    return NewEvent(
        stream=f"run:{run_id}",
        type="run.created",
        actor=ORCH,
        payload={
            "run_id": run_id,
            "goal": "ship it",
            "repo_path": "C:/repo",
            "base_commit": "abc123",
            "budget": {"max_steps": 10},
        },
    )


def _task_created(task_id: str, blocked_by: list[str] | None = None) -> NewEvent:
    spec = TaskSpec(
        id=task_id,
        run_id="r1",
        title=f"task {task_id}",
        instructions="do it",
        write_set=["src/**"],
        blocked_by=blocked_by or [],
    )
    return NewEvent(
        stream=f"task:{task_id}",
        type="task.created",
        actor=ORCH,
        payload={"spec": spec.model_dump(mode="json")},
    )


def _change(task_id: str, frm: TaskState, to: TaskState, revision: int) -> NewEvent:
    change = TaskStateChange(task_id=task_id, from_state=frm, to_state=to, revision=revision)
    return NewEvent(
        stream=f"task:{task_id}",
        type="task.state_changed",
        actor=ORCH,
        payload=change.model_dump(mode="json"),
    )


def _task(conn: sqlite3.Connection, task_id: str = "t1") -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row is not None
    return row


def _event_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])


def _dump(conn: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]  # noqa: S608
        for table in ("runs", "tasks", "task_deps")
    }


def test_created_events_insert_rows(conn: sqlite3.Connection) -> None:
    _store(conn).append([_run_created(), _task_created("t1"), _task_created("t2", ["t1"])])

    run = conn.execute("SELECT * FROM runs WHERE id = 'r1'").fetchone()
    assert (run["goal"], run["status"], run["base_commit"]) == ("ship it", "running", "abc123")

    task = _task(conn)
    assert (task["state"], task["revision"], task["attempts"]) == ("PENDING", 1, 0)
    assert task["worker"] == "native"
    assert TaskSpec.model_validate_json(task["spec_json"]).id == "t1"
    assert task["write_set_json"] == '["src/**"]'

    deps = conn.execute("SELECT task_id, blocker_id FROM task_deps").fetchall()
    assert [tuple(d) for d in deps] == [("t2", "t1")]


def test_run_finished_updates_status(conn: sqlite3.Connection) -> None:
    store = _store(conn)
    store.append([_run_created()])
    store.append(
        [
            NewEvent(
                stream="run:r1",
                type="run.finished",
                actor=ORCH,
                payload={"run_id": "r1", "status": "done"},
            )
        ]
    )
    assert conn.execute("SELECT status FROM runs WHERE id = 'r1'").fetchone()[0] == "done"


def test_happy_path(conn: sqlite3.Connection) -> None:
    store = _store(conn)
    store.append([_run_created(), _task_created("t1")])
    current, revision = S.PENDING, 1
    for target in HAPPY_PATH:
        revision += 1
        store.append([_change("t1", current, target, revision)])
        current = target
    task = _task(conn)
    assert (task["state"], task["revision"], task["attempts"]) == ("DONE", 7, 1)


def test_illegal_transition_rejected_without_trace(conn: sqlite3.Connection) -> None:
    store = _store(conn)
    store.append([_run_created(), _task_created("t1")])
    for revision, (frm, to) in enumerate(
        [(S.PENDING, S.READY), (S.READY, S.LEASED), (S.LEASED, S.RUNNING)], start=2
    ):
        store.append([_change("t1", frm, to, revision)])
    before = _event_count(conn)

    with pytest.raises(IllegalTransitionError):
        store.append([_change("t1", S.RUNNING, S.DONE, 5)])

    assert _event_count(conn) == before
    task = _task(conn)
    assert (task["state"], task["revision"]) == ("RUNNING", 4)


@pytest.mark.parametrize(
    ("frm", "revision"),
    [(S.READY, 2), (S.PENDING, 1), (S.PENDING, 3)],
    ids=["stale-from-state", "same-revision", "skipped-revision"],
)
def test_stale_change_rejected(conn: sqlite3.Connection, frm: TaskState, revision: int) -> None:
    store = _store(conn)
    store.append([_run_created(), _task_created("t1")])
    before = _event_count(conn)
    with pytest.raises(StaleTaskStateError):
        store.append([_change("t1", frm, S.READY, revision)])
    assert _event_count(conn) == before
    assert _task(conn)["state"] == "PENDING"


def test_rebuild_reproduces_projection(conn: sqlite3.Connection) -> None:
    store = _store(conn)
    store.append(
        [_run_created(), _task_created("t1"), _task_created("t2", ["t1"]), _task_created("t3")]
    )
    store.append([_change("t1", S.PENDING, S.READY, 2), _change("t1", S.READY, S.LEASED, 3)])
    store.append([_change("t1", S.LEASED, S.RUNNING, 4), _change("t3", S.PENDING, S.CANCELLED, 2)])
    before = _dump(conn)

    conn.execute("UPDATE tasks SET state = 'FAILED'")  # corrupt the projection on purpose
    rebuild_projections(conn)

    assert _dump(conn) == before
    assert not conn.in_transaction


@settings(max_examples=150, deadline=None)
@given(st.lists(st.sampled_from(list(TaskState)), max_size=25))
def test_random_transitions_respect_the_table(targets: list[TaskState]) -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        apply_migrations(conn)
        store = _store(conn)
        store.append([_run_created(), _task_created("t1")])

        current, revision = S.PENDING, 1
        for target in targets:
            allowed = can_transition(current, target)
            try:
                store.append([_change("t1", current, target, revision + 1)])
            except IllegalTransitionError:
                assert not allowed
            else:
                assert allowed
                assert current not in TERMINAL
                current, revision = target, revision + 1

            row = _task(conn)
            assert TaskState(row["state"]) == current
            assert row["revision"] == revision
    finally:
        conn.close()
