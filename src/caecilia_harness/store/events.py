"""Append-only event log: the single entry point for state changes (ADR-0002)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

from caecilia_harness.core.clock import Clock, SystemClock
from caecilia_harness.core.models import (
    Actor,
    Event,
    Lease,
    NewEvent,
    OutboxItem,
    OutboxRecord,
    TaskRow,
    TaskSpec,
)
from caecilia_harness.core.states import TaskState
from caecilia_harness.store import leases
from caecilia_harness.store.errors import StoreError, StreamConflictError, UnknownTaskError

Projector = Callable[[sqlite3.Connection, Event], None]

_COLUMNS = (
    "seq, event_id, stream, stream_seq, type, schema_version, ts, actor, causation_id, "
    "correlation_id, idempotency_key, payload_json, payload_ref"
)

_TASK_SELECT = "SELECT id, run_id, state, revision, attempts, spec_json FROM tasks"
_OUTBOX_STATUSES = frozenset({"pending", "done", "failed", "cancelled"})
_STORE_ACTOR = Actor(kind="orchestrator", id="store")


def _row_to_task(row: sqlite3.Row) -> TaskRow:
    return TaskRow(
        id=row["id"],
        run_id=row["run_id"],
        state=TaskState(row["state"]),
        revision=row["revision"],
        attempts=row["attempts"],
        spec=TaskSpec.model_validate_json(row["spec_json"]),
    )


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        seq=row["seq"],
        event_id=row["event_id"],
        stream=row["stream"],
        stream_seq=row["stream_seq"],
        type=row["type"],
        schema_version=row["schema_version"],
        ts=datetime.fromisoformat(row["ts"]),
        actor=Actor.model_validate_json(row["actor"]),
        causation_id=row["causation_id"],
        correlation_id=row["correlation_id"],
        idempotency_key=row["idempotency_key"],
        payload=json.loads(row["payload_json"]),
        payload_ref=row["payload_ref"],
    )


class SqliteEventStore:
    """`core.ports.StateStore` backed by the `events` table.

    The connection must be opened with `isolation_level=None` (see `store.db.open_db`); this
    class manages its own `BEGIN IMMEDIATE` transactions.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        projectors: Sequence[Projector] = (),
        clock: Clock | None = None,
    ) -> None:
        self._conn = conn
        self._clock: Clock = clock or SystemClock()
        # The lease projector is always on: lease methods below rely on it.
        self._projectors = (leases.make_lease_projector(self._clock), *projectors)

    def append(
        self,
        events: Sequence[NewEvent],
        expect: Mapping[str, int] | None = None,
        outbox: Sequence[OutboxItem] = (),
    ) -> list[Event]:
        """Append `events` in one transaction and return them as stored.

        Events whose `idempotency_key` is already stored are not written again; the stored
        event is returned in their place. `expect` maps stream -> last known `stream_seq`
        (0 = empty stream); any mismatch rolls back the whole batch. `outbox` items are
        inserted in the same transaction; an existing `idempotency_key` is left untouched.
        """
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stream, expected in (expect or {}).items():
                actual = self._last_stream_seq(stream)
                if actual != expected:
                    raise StreamConflictError(stream, expected, actual)

            result: list[Event] = []
            next_seq: dict[str, int] = {}
            for new in events:
                if new.idempotency_key is not None:
                    existing = self._by_idempotency_key(new.idempotency_key)
                    if existing is not None:
                        result.append(existing)
                        continue
                if new.stream not in next_seq:
                    next_seq[new.stream] = self._last_stream_seq(new.stream) + 1
                event = self._insert(new, next_seq[new.stream])
                next_seq[new.stream] += 1
                for project in self._projectors:
                    project(conn, event)
                result.append(event)
            conn.executemany(
                "INSERT OR IGNORE INTO outbox (idempotency_key, kind, payload_json) "
                "VALUES (?, ?, ?)",
                [(item.idempotency_key, item.kind, json.dumps(item.payload)) for item in outbox],
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return result

    def read_stream(self, stream: str, after: int = 0) -> list[Event]:
        """Return events of `stream` with `stream_seq > after`, in stream order."""
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM events WHERE stream = ? AND stream_seq > ? "  # noqa: S608
            "ORDER BY stream_seq",
            (stream, after),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def read_all(self, after_seq: int = 0, limit: int | None = None) -> list[Event]:
        """Return events with global `seq > after_seq`, in global order."""
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM events WHERE seq > ? ORDER BY seq LIMIT ?",  # noqa: S608
            (after_seq, -1 if limit is None else limit),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def cas_document(self, doc_id: str, expected_revision: int, content_ref: str) -> int:
        raise NotImplementedError("M3")

    # --- queries ---------------------------------------------------------------------------

    def get_task(self, task_id: str) -> TaskRow:
        row = self._conn.execute(f"{_TASK_SELECT} WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise UnknownTaskError(task_id)
        return _row_to_task(row)

    def list_tasks(
        self, run_id: str | None, states: Sequence[TaskState] | None = None
    ) -> list[TaskRow]:
        if run_id is None:
            rows = self._conn.execute(f"{_TASK_SELECT} ORDER BY rowid").fetchall()
        else:
            rows = self._conn.execute(
                f"{_TASK_SELECT} WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
        tasks = [_row_to_task(row) for row in rows]
        return tasks if states is None else [t for t in tasks if t.state in set(states)]

    # --- outbox and artifacts --------------------------------------------------------------
    # Bookkeeping tables written outside `append`: they record delivery of side effects and
    # artifact metadata, not task state.

    def pending_outbox(self) -> list[OutboxRecord]:
        rows = self._conn.execute(
            "SELECT id, idempotency_key, kind, payload_json, status, attempts FROM outbox "
            "WHERE status = 'pending' ORDER BY id"
        ).fetchall()
        return [
            OutboxRecord(
                id=row["id"],
                idempotency_key=row["idempotency_key"],
                kind=row["kind"],
                payload=json.loads(row["payload_json"]),
                status=row["status"],
                attempts=row["attempts"],
            )
            for row in rows
        ]

    def mark_outbox(self, outbox_id: int, status: str) -> None:
        if status not in _OUTBOX_STATUSES:
            raise ValueError(f"unknown outbox status {status!r}")
        # A processing attempt ends in done/failed; resetting to pending does not count one.
        cursor = self._conn.execute(
            "UPDATE outbox SET status = ?, attempts = attempts + ? WHERE id = ?",
            (status, int(status != "pending"), outbox_id),
        )
        if cursor.rowcount != 1:
            raise StoreError(f"no outbox item with id {outbox_id}")

    def register_artifact(
        self,
        hash: str,
        kind: str,
        task_id: str | None,
        path: str,
        bytes: int,
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO artifacts (hash, kind, task_id, path, bytes) "
            "VALUES (?, ?, ?, ?, ?)",
            (hash, kind, task_id, path, bytes),
        )

    # --- leases ------------------------------------------------------------------------------

    def acquire_lease(self, task_id: str, holder: str, attempt: int, ttl_s: float) -> Lease:
        expires_at = leases.expiry(self._clock, ttl_s)
        self.append(
            [leases.lease_acquired_event(task_id, holder, attempt, expires_at, _STORE_ACTOR)]
        )
        return next(lease for lease in self.list_leases() if lease.task_id == task_id)

    def renew_lease(self, task_id: str, holder: str, ttl_s: float) -> bool:
        return leases.renew(self._conn, task_id, holder, leases.expiry(self._clock, ttl_s))

    def release_lease(self, task_id: str, holder: str) -> None:
        self.append([leases.lease_released_event(task_id, holder, _STORE_ACTOR)])

    def list_leases(self) -> list[Lease]:
        return leases.list_all(self._conn)

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def _last_stream_seq(self, stream: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(stream_seq), 0) FROM events WHERE stream = ?", (stream,)
        ).fetchone()
        return int(row[0])

    def _by_idempotency_key(self, key: str) -> Event | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM events WHERE idempotency_key = ?",  # noqa: S608
            (key,),
        ).fetchone()
        return None if row is None else _row_to_event(row)

    def _insert(self, new: NewEvent, stream_seq: int) -> Event:
        event_id = uuid.uuid4().hex
        ts = datetime.now(UTC)
        cursor = self._conn.execute(
            "INSERT INTO events (event_id, stream, stream_seq, type, schema_version, ts, actor, "
            "causation_id, correlation_id, idempotency_key, payload_json, payload_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                new.stream,
                stream_seq,
                new.type,
                new.schema_version,
                ts.isoformat(),
                new.actor.model_dump_json(),
                new.causation_id,
                new.correlation_id,
                new.idempotency_key,
                json.dumps(new.payload),
                new.payload_ref,
            ),
        )
        assert cursor.lastrowid is not None  # noqa: S101 - INSERT into a rowid table
        return Event(
            **new.model_dump(),
            event_id=event_id,
            seq=cursor.lastrowid,
            stream_seq=stream_seq,
            ts=ts,
        )
