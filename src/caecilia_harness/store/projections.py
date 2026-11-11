"""Keep `runs`, `tasks` and `task_deps` as projections of the event log.

Projectors run inside the append transaction (see `SqliteEventStore`), so raising here rolls
back the whole batch: an illegal or stale state change never reaches the log.
"""

from __future__ import annotations

import json
import sqlite3

from caecilia_harness.core.models import Event, TaskSpec, TaskStateChange
from caecilia_harness.core.states import TaskState, can_transition
from caecilia_harness.store.errors import (
    IllegalTransitionError,
    ProjectionError,
    StaleTaskStateError,
)
from caecilia_harness.store.events import Projector, SqliteEventStore

_REBUILD_PAGE = 1000


def project_runs_and_tasks(conn: sqlite3.Connection, event: Event) -> None:
    """Apply one event to the run/task projections. Unknown event types are ignored."""
    handler = _HANDLERS.get(event.type)
    if handler is not None:
        handler(conn, event)


def rebuild_projections(conn: sqlite3.Connection) -> None:
    """Clear `runs`, `tasks` and `task_deps` and replay every event, in one transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Other tables (leases, pins, approvals) reference tasks; replay recreates the same
        # ids, so foreign keys are checked at COMMIT instead of during the DELETE.
        conn.execute("PRAGMA defer_foreign_keys=ON")
        conn.execute("DELETE FROM task_deps")
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM runs")
        reader = SqliteEventStore(conn)
        after = 0
        while page := reader.read_all(after_seq=after, limit=_REBUILD_PAGE):
            for event in page:
                project_runs_and_tasks(conn, event)
            after = page[-1].seq
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _run_created(conn: sqlite3.Connection, event: Event) -> None:
    p = event.payload
    conn.execute(
        "INSERT INTO runs (id, goal, status, repo_path, base_commit, budget_json, created_at) "
        "VALUES (?, ?, 'running', ?, ?, ?, ?)",
        (
            p["run_id"],
            p["goal"],
            p["repo_path"],
            p["base_commit"],
            json.dumps(p.get("budget", {})),
            event.ts.isoformat(),
        ),
    )


def _run_finished(conn: sqlite3.Connection, event: Event) -> None:
    p = event.payload
    cursor = conn.execute("UPDATE runs SET status = ? WHERE id = ?", (p["status"], p["run_id"]))
    if cursor.rowcount != 1:
        raise ProjectionError(f"run.finished for unknown run {p['run_id']!r}")


def _task_created(conn: sqlite3.Connection, event: Event) -> None:
    spec = TaskSpec.model_validate(event.payload["spec"])
    conn.execute(
        "INSERT INTO tasks (id, run_id, title, spec_json, state, revision, worker, write_set_json) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
        (
            spec.id,
            spec.run_id,
            spec.title,
            spec.model_dump_json(),
            TaskState.PENDING.value,
            spec.worker,
            json.dumps(spec.write_set),
        ),
    )
    conn.executemany(
        "INSERT INTO task_deps (task_id, blocker_id) VALUES (?, ?)",
        [(spec.id, blocker) for blocker in spec.blocked_by],
    )


def _task_state_changed(conn: sqlite3.Connection, event: Event) -> None:
    change = TaskStateChange.model_validate(event.payload)
    row = conn.execute(
        "SELECT state, revision FROM tasks WHERE id = ?", (change.task_id,)
    ).fetchone()
    if row is None:
        raise ProjectionError(f"task.state_changed for unknown task {change.task_id!r}")
    current, revision = TaskState(row[0]), int(row[1])

    if change.from_state != current:
        raise StaleTaskStateError(
            f"task {change.task_id!r}: change assumes {change.from_state}, task is {current}"
        )
    if change.revision != revision + 1:
        raise StaleTaskStateError(
            f"task {change.task_id!r}: change has revision {change.revision}, "
            f"expected {revision + 1}"
        )
    if not can_transition(change.from_state, change.to_state):
        raise IllegalTransitionError(
            f"task {change.task_id!r}: {change.from_state} -> {change.to_state} is not allowed"
        )

    # An attempt starts when the worker confirms it is running, not when the lease is taken.
    started = change.from_state == TaskState.LEASED and change.to_state == TaskState.RUNNING
    conn.execute(
        "UPDATE tasks SET state = ?, revision = ?, attempts = attempts + ? WHERE id = ?",
        (change.to_state.value, change.revision, int(started), change.task_id),
    )


_HANDLERS: dict[str, Projector] = {
    "run.created": _run_created,
    "run.finished": _run_finished,
    "task.created": _task_created,
    "task.state_changed": _task_state_changed,
}
