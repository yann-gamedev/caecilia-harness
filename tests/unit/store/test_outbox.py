"""Transactional outbox, task queries and artifact registration (backlog M1-10)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from caecilia_harness.core.models import Actor, NewEvent, OutboxItem, TaskSpec
from caecilia_harness.core.ports import StateStore
from caecilia_harness.core.states import TaskState
from caecilia_harness.paths import db_path
from caecilia_harness.store.db import open_db
from caecilia_harness.store.errors import StreamConflictError, UnknownTaskError
from caecilia_harness.store.events import SqliteEventStore
from caecilia_harness.store.projections import project_runs_and_tasks

ORCH = Actor(kind="orchestrator", id="o")


@pytest.fixture
def store() -> Iterator[SqliteEventStore]:
    s = SqliteEventStore(open_db(db_path()), projectors=[project_runs_and_tasks])
    try:
        yield s
    finally:
        s.close()


def _ev(stream: str = "task:t1") -> NewEvent:
    return NewEvent(stream=stream, type="test.happened", actor=ORCH)


def _item(key: str = "t1:1:spawn") -> OutboxItem:
    return OutboxItem(idempotency_key=key, kind="spawn_worker", payload={"task_id": "t1"})


def _rows(store: SqliteEventStore) -> list[sqlite3.Row]:
    return store._conn.execute("SELECT * FROM outbox").fetchall()


def test_still_satisfies_port(store: SqliteEventStore) -> None:
    assert isinstance(store, StateStore)


def test_outbox_is_written_with_the_events(store: SqliteEventStore) -> None:
    store.append([_ev()], outbox=[_item()])
    [record] = store.pending_outbox()
    assert (record.idempotency_key, record.kind, record.status, record.attempts) == (
        "t1:1:spawn",
        "spawn_worker",
        "pending",
        0,
    )
    assert record.payload == {"task_id": "t1"}


def test_same_outbox_key_twice_is_one_row(store: SqliteEventStore) -> None:
    store.append([_ev()], outbox=[_item()])
    store.append([_ev()], outbox=[_item(), _item()])
    assert len(_rows(store)) == 1


def test_outbox_rolls_back_with_a_failed_append(store: SqliteEventStore) -> None:
    with pytest.raises(StreamConflictError):
        store.append([_ev()], expect={"task:t1": 5}, outbox=[_item()])
    assert _rows(store) == []


def test_mark_outbox(store: SqliteEventStore) -> None:
    store.append([], outbox=[_item("a"), _item("b")])
    first = store.pending_outbox()[0]
    store.mark_outbox(first.id, "done")
    assert [r.idempotency_key for r in store.pending_outbox()] == ["b"]
    store.mark_outbox(first.id, "pending")
    again = store.pending_outbox()
    assert [r.idempotency_key for r in again] == ["a", "b"]
    assert again[0].attempts == 1
    with pytest.raises(ValueError):
        store.mark_outbox(first.id, "exploded")


def test_task_queries(store: SqliteEventStore) -> None:
    run = NewEvent(
        stream="run:r1",
        type="run.created",
        actor=ORCH,
        payload={"run_id": "r1", "goal": "g", "repo_path": ".", "base_commit": "b"},
    )
    tasks = [
        NewEvent(
            stream=f"task:{tid}",
            type="task.created",
            actor=ORCH,
            payload={
                "spec": TaskSpec(id=tid, run_id="r1", title=tid, instructions="x").model_dump()
            },
        )
        for tid in ("t1", "t2")
    ]
    store.append([run, *tasks])

    t1 = store.get_task("t1")
    assert (t1.state, t1.revision, t1.attempts, t1.spec.title) == (TaskState.PENDING, 1, 0, "t1")
    assert [t.id for t in store.list_tasks("r1")] == ["t1", "t2"]
    assert store.list_tasks("r1", states=[TaskState.READY]) == []
    assert store.list_tasks("other") == []
    with pytest.raises(UnknownTaskError):
        store.get_task("missing")


def test_register_artifact_is_idempotent(store: SqliteEventStore) -> None:
    store.register_artifact("ab" * 32, "patch", None, "ab/abab", 10)
    store.register_artifact("ab" * 32, "patch", None, "ab/abab", 10)
    rows = store._conn.execute("SELECT hash, kind, bytes FROM artifacts").fetchall()
    assert [tuple(r) for r in rows] == [("ab" * 32, "patch", 10)]
