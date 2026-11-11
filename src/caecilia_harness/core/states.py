"""Task state machine: the single authoritative transition table.

See docs/architecture.md ("Task state machine"). Any code that changes a task's state must
check `can_transition` first; the store's projection rejects anything else.
"""

from __future__ import annotations

from enum import StrEnum


class TaskState(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    AWAITING_GATE = "AWAITING_GATE"
    INTEGRATING = "INTEGRATING"
    DONE = "DONE"
    REWORK = "REWORK"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    STALE = "STALE"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    RECOVERING = "RECOVERING"
    CANCELLED = "CANCELLED"


S = TaskState

TERMINAL: frozenset[TaskState] = frozenset({S.DONE, S.FAILED, S.CANCELLED})

_FORWARD: dict[TaskState, frozenset[TaskState]] = {
    S.PENDING: frozenset({S.READY}),
    S.READY: frozenset({S.LEASED}),
    S.LEASED: frozenset({S.RUNNING, S.READY, S.RECOVERING}),
    S.RUNNING: frozenset({S.AWAITING_GATE, S.STALE, S.RETRY_WAIT, S.FAILED, S.RECOVERING}),
    S.AWAITING_GATE: frozenset({S.INTEGRATING, S.STALE}),
    S.INTEGRATING: frozenset({S.DONE, S.REWORK, S.NEEDS_HUMAN}),
    S.REWORK: frozenset({S.READY}),
    S.STALE: frozenset({S.READY, S.REWORK}),
    S.RETRY_WAIT: frozenset({S.READY}),
    S.RECOVERING: frozenset({S.READY, S.NEEDS_HUMAN}),
    S.NEEDS_HUMAN: frozenset({S.READY}),
}

#: Allowed transitions. Every non-terminal state may also go to CANCELLED.
TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    state: (frozenset() if state in TERMINAL else _FORWARD.get(state, frozenset()) | {S.CANCELLED})
    for state in TaskState
}


def can_transition(current: TaskState, target: TaskState) -> bool:
    """Return True if moving a task from `current` to `target` is allowed."""
    return target in TRANSITIONS[current]
