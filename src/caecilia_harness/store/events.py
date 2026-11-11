"""Append-only event log: the single entry point for state changes (ADR-0002)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

from caecilia_harness.core.models import Actor, Event, NewEvent
from caecilia_harness.store.errors import StreamConflictError

Projector = Callable[[sqlite3.Connection, Event], None]

_COLUMNS = (
    "seq, event_id, stream, stream_seq, type, schema_version, ts, actor, causation_id, "
    "correlation_id, idempotency_key, payload_json, payload_ref"
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

    def __init__(self, conn: sqlite3.Connection, projectors: Sequence[Projector] = ()) -> None:
        self._conn = conn
        self._projectors = tuple(projectors)

    def append(
        self, events: Sequence[NewEvent], expect: Mapping[str, int] | None = None
    ) -> list[Event]:
        """Append `events` in one transaction and return them as stored.

        Events whose `idempotency_key` is already stored are not written again; the stored
        event is returned in their place. `expect` maps stream -> last known `stream_seq`
        (0 = empty stream); any mismatch rolls back the whole batch.
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
