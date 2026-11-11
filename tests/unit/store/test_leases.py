"""Task leases with acquire / renew / release / expire (backlog M1-11)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from caecilia_harness.core.models import Actor, NewEvent, TaskSpec
from caecilia_harness.paths import db_path
from caecilia_harness.store.db import open_db
from caecilia_harness.store.events import SqliteEventStore
from caecilia_harness.store.leases import LeaseHeldError
from caecilia_harness.store.projections import project_runs_and_tasks

ORCH = Actor(kind="orchestrator", id="o")


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock: FakeClock) -> Iterator[SqliteEventStore]:
    s = SqliteEventStore(open_db(db_path()), projectors=[project_runs_and_tasks], clock=clock)
    run = NewEvent(
        stream="run:r1",
        type="run.created",
        actor=ORCH,
        payload={"run_id": "r1", "goal": "g", "repo_path": ".", "base_commit": "b"},
    )
    spec = TaskSpec(id="t1", run_id="r1", title="t", instructions="x")
    task = NewEvent(
        stream="task:t1", type="task.created", actor=ORCH, payload={"spec": spec.model_dump()}
    )
    s.append([run, task])
    try:
        yield s
    finally:
        s.close()


def _types(store: SqliteEventStore) -> list[str]:
    return [e.type for e in store.read_stream("task:t1")]


def test_acquire_records_event_and_row(store: SqliteEventStore, clock: FakeClock) -> None:
    lease = store.acquire_lease("t1", "host:1:boot-a", attempt=1, ttl_s=30)
    assert (lease.holder, lease.attempt) == ("host:1:boot-a", 1)
    assert lease.expires_at == clock.now() + timedelta(seconds=30)
    assert store.list_leases() == [lease]
    assert _types(store)[-1] == "task.lease_acquired"


def test_second_holder_is_refused_while_active(store: SqliteEventStore) -> None:
    store.acquire_lease("t1", "host:1:boot-a", 1, 30)
    events_before = len(_types(store))
    with pytest.raises(LeaseHeldError):
        store.acquire_lease("t1", "host:2:boot-b", 1, 30)
    assert len(_types(store)) == events_before  # rejected acquire leaves no event
    assert [lease.holder for lease in store.list_leases()] == ["host:1:boot-a"]


def test_expired_lease_can_be_taken_over(store: SqliteEventStore, clock: FakeClock) -> None:
    store.acquire_lease("t1", "host:1:boot-a", 1, 30)
    clock.advance(31)
    store.acquire_lease("t1", "host:2:boot-b", 2, 30)
    assert [lease.holder for lease in store.list_leases()] == ["host:2:boot-b"]


def test_renew_extends_expiry(store: SqliteEventStore, clock: FakeClock) -> None:
    store.acquire_lease("t1", "host:1:boot-a", 1, 30)
    clock.advance(20)
    assert store.renew_lease("t1", "host:1:boot-a", 30) is True
    [lease] = store.list_leases()
    assert lease.expires_at == clock.now() + timedelta(seconds=30)
    clock.advance(20)  # 40 s after acquiring, but only 20 s after the renewal
    with pytest.raises(LeaseHeldError):
        store.acquire_lease("t1", "host:2:boot-b", 1, 30)


def test_renew_by_non_holder_fails(store: SqliteEventStore) -> None:
    store.acquire_lease("t1", "host:1:boot-a", 1, 30)
    assert store.renew_lease("t1", "host:2:boot-b", 30) is False
    assert store.renew_lease("missing", "host:1:boot-a", 30) is False


def test_release_removes_lease(store: SqliteEventStore) -> None:
    store.acquire_lease("t1", "host:1:boot-a", 1, 30)
    store.release_lease("t1", "host:1:boot-a")
    assert store.list_leases() == []
    assert _types(store)[-1] == "task.lease_released"
    store.acquire_lease("t1", "host:2:boot-b", 1, 30)  # free again


def test_expired_event_removes_lease(store: SqliteEventStore) -> None:
    store.acquire_lease("t1", "host:1:boot-a", 1, 30)
    store.append(
        [
            NewEvent(
                stream="task:t1",
                type="task.lease_expired",
                actor=ORCH,
                payload={"task_id": "t1", "holder": "host:1:boot-a"},
            )
        ]
    )
    assert store.list_leases() == []
