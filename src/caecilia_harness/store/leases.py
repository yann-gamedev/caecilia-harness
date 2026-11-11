"""Task leases: who may run a task, and until when.

The `leases` table is maintained from `task.lease_acquired` / `task.lease_released` /
`task.lease_expired` events inside the append transaction, so a lease can be taken atomically
with the READY -> LEASED transition. Heartbeat renewals update `expires_at` directly: they are
frequent and not durable facts (ADR-0011).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

from caecilia_harness.core.clock import Clock
from caecilia_harness.core.models import Actor, Event, Lease, NewEvent
from caecilia_harness.store.errors import StoreError

LEASE_ACQUIRED = "task.lease_acquired"
LEASE_RELEASED = "task.lease_released"
LEASE_EXPIRED = "task.lease_expired"


class LeaseHeldError(StoreError):
    """Another holder's lease on the task is still active."""


def lease_acquired_event(
    task_id: str, holder: str, attempt: int, expires_at: datetime, actor: Actor
) -> NewEvent:
    return NewEvent(
        stream=f"task:{task_id}",
        type=LEASE_ACQUIRED,
        actor=actor,
        payload={
            "task_id": task_id,
            "holder": holder,
            "attempt": attempt,
            "pid": os.getpid(),
            "expires_at": expires_at.isoformat(),
        },
    )


def lease_released_event(task_id: str, holder: str, actor: Actor) -> NewEvent:
    return NewEvent(
        stream=f"task:{task_id}",
        type=LEASE_RELEASED,
        actor=actor,
        payload={"task_id": task_id, "holder": holder},
    )


def make_lease_projector(clock: Clock) -> Callable[[sqlite3.Connection, Event], None]:
    """Projector keeping `leases` in step with lease events; `clock` decides what is active."""

    def project(conn: sqlite3.Connection, event: Event) -> None:
        p = event.payload
        if event.type == LEASE_ACQUIRED:
            row = conn.execute(
                "SELECT holder, expires_at FROM leases WHERE task_id = ?", (p["task_id"],)
            ).fetchone()
            if (
                row is not None
                and row[0] != p["holder"]
                and datetime.fromisoformat(row[1]) > clock.now()
            ):
                raise LeaseHeldError(f"task {p['task_id']!r} is leased by {row[0]} until {row[1]}")
            conn.execute(
                "INSERT OR REPLACE INTO leases (task_id, holder, pid, attempt, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (p["task_id"], p["holder"], p.get("pid"), p["attempt"], p["expires_at"]),
            )
        elif event.type == LEASE_RELEASED:
            conn.execute(
                "DELETE FROM leases WHERE task_id = ? AND holder = ?", (p["task_id"], p["holder"])
            )
        elif event.type == LEASE_EXPIRED:
            conn.execute("DELETE FROM leases WHERE task_id = ?", (p["task_id"],))

    return project


def renew(conn: sqlite3.Connection, task_id: str, holder: str, expires_at: datetime) -> bool:
    cursor = conn.execute(
        "UPDATE leases SET expires_at = ? WHERE task_id = ? AND holder = ?",
        (expires_at.isoformat(), task_id, holder),
    )
    return cursor.rowcount == 1


def list_all(conn: sqlite3.Connection) -> list[Lease]:
    rows = conn.execute(
        "SELECT task_id, holder, pid, attempt, expires_at FROM leases ORDER BY task_id"
    ).fetchall()
    return [
        Lease(
            task_id=row[0],
            holder=row[1],
            pid=row[2],
            attempt=row[3],
            expires_at=datetime.fromisoformat(row[4]),
        )
        for row in rows
    ]


def expiry(clock: Clock, ttl_s: float) -> datetime:
    return clock.now() + timedelta(seconds=ttl_s)
