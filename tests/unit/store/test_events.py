"""Atomic event append with per-stream CAS and idempotency keys (backlog M1-02)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC

import pytest

from caecilia_harness.core.models import Actor, Event, NewEvent
from caecilia_harness.core.ports import StateStore
from caecilia_harness.paths import db_path
from caecilia_harness.store.db import open_db
from caecilia_harness.store.errors import StoreError, StreamConflictError
from caecilia_harness.store.events import SqliteEventStore

ORCH = Actor(kind="orchestrator", id="orch-1")


def _ev(stream: str, type_: str = "test.happened", **kw: object) -> NewEvent:
    return NewEvent(stream=stream, type=type_, actor=ORCH, **kw)  # type: ignore[arg-type]


def _count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = open_db(db_path())
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def store(conn: sqlite3.Connection) -> SqliteEventStore:
    return SqliteEventStore(conn)


def test_store_satisfies_port(store: SqliteEventStore) -> None:
    assert isinstance(store, StateStore)


def test_batch_to_one_stream_numbers_sequentially(store: SqliteEventStore) -> None:
    out = store.append([_ev("task:t1"), _ev("task:t1"), _ev("task:t1")])
    assert [e.stream_seq for e in out] == [1, 2, 3]
    seqs = [e.seq for e in out]
    assert seqs == sorted(seqs)
    assert seqs == list(range(seqs[0], seqs[0] + 3))
    assert len({e.event_id for e in out}) == 3
    assert all(e.ts.tzinfo is not None and e.ts.utcoffset() == UTC.utcoffset(None) for e in out)


def test_two_streams_in_one_batch_start_at_one(store: SqliteEventStore) -> None:
    out = store.append([_ev("task:a"), _ev("task:b"), _ev("task:a")])
    assert [(e.stream, e.stream_seq) for e in out] == [("task:a", 1), ("task:b", 1), ("task:a", 2)]


def test_expect_acts_as_compare_and_set(store: SqliteEventStore, conn: sqlite3.Connection) -> None:
    store.append([_ev("task:t1")], expect={"task:t1": 0})
    before = _count(conn)
    with pytest.raises(StreamConflictError) as info:
        store.append([_ev("task:t1")], expect={"task:t1": 0})
    err = info.value
    assert (err.stream, err.expected, err.actual) == ("task:t1", 0, 1)
    assert isinstance(err, StoreError)
    assert _count(conn) == before
    assert not conn.in_transaction


def test_conflict_writes_nothing_from_the_batch(
    store: SqliteEventStore, conn: sqlite3.Connection
) -> None:
    store.append([_ev("task:b")])
    with pytest.raises(StreamConflictError):
        store.append(
            [_ev("task:a"), _ev("task:b"), _ev("task:c")],
            expect={"task:a": 0, "task:b": 0},
        )
    assert _count(conn) == 1
    assert store.read_stream("task:a") == []
    assert store.read_stream("task:c") == []


def test_idempotency_key_returns_existing_event(
    store: SqliteEventStore, conn: sqlite3.Connection
) -> None:
    [first] = store.append([_ev("task:t1", idempotency_key="k1")])
    again = store.append([_ev("task:t1"), _ev("task:t1", idempotency_key="k1")])
    assert again[1] == first
    assert again[0].stream_seq == 2
    assert _count(conn) == 2


def test_duplicate_key_inside_one_batch_is_written_once(
    store: SqliteEventStore, conn: sqlite3.Connection
) -> None:
    out = store.append([_ev("task:t1", idempotency_key="k"), _ev("task:t1", idempotency_key="k")])
    assert out[0] == out[1]
    assert _count(conn) == 1


def test_projectors_see_new_events_and_failure_rolls_back(conn: sqlite3.Connection) -> None:
    seen: list[tuple[int, bool]] = []

    def record(c: sqlite3.Connection, event: Event) -> None:
        seen.append((event.stream_seq, c.in_transaction))

    SqliteEventStore(conn, projectors=[record]).append(
        [_ev("task:t1", idempotency_key="k"), _ev("task:t1")]
    )
    assert seen == [(1, True), (2, True)]

    seen.clear()
    SqliteEventStore(conn, projectors=[record]).append([_ev("task:t1", idempotency_key="k")])
    assert seen == []  # replays are not projected again

    def boom(c: sqlite3.Connection, event: Event) -> None:
        if event.stream == "task:bad":
            raise RuntimeError("projector failed")

    failing = SqliteEventStore(conn, projectors=[boom])
    with pytest.raises(RuntimeError):
        failing.append([_ev("task:ok"), _ev("task:bad")])
    assert _count(conn) == 2
    assert not conn.in_transaction


def test_read_stream_after(store: SqliteEventStore) -> None:
    store.append([_ev("task:t1") for _ in range(4)] + [_ev("task:other")])
    after = store.read_stream("task:t1", after=2)
    assert [e.stream_seq for e in after] == [3, 4]
    assert all(e.stream == "task:t1" for e in after)


def test_read_all_global_order(store: SqliteEventStore) -> None:
    written = store.append([_ev("task:a"), _ev("task:b"), _ev("task:a")])
    assert store.read_all() == written
    assert store.read_all(after_seq=written[0].seq) == written[1:]
    assert store.read_all(limit=2) == written[:2]


def test_round_trip_equals_append_result(store: SqliteEventStore) -> None:
    [written] = store.append(
        [
            _ev(
                "run:r1",
                "run.created",
                payload={"goal": "x", "n": 1, "nested": {"ok": True, "items": [1, 2]}},
                payload_ref="abc123",
                causation_id="c1",
                correlation_id="r1",
                idempotency_key="run:r1:created",
                schema_version=2,
            )
        ]
    )
    assert store.read_stream("run:r1") == [written]
    assert written.actor == ORCH


def test_cas_document_not_implemented_yet(store: SqliteEventStore) -> None:
    with pytest.raises(NotImplementedError):
        store.cas_document("doc", 0, "ref")
