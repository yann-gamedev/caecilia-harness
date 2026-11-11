from __future__ import annotations

import itertools
from collections import deque

from caecilia_harness.core.states import TERMINAL, TRANSITIONS, TaskState, can_transition


def _reachable_from(start: TaskState) -> set[TaskState]:
    seen = {start}
    queue = deque([start])
    while queue:
        for nxt in TRANSITIONS[queue.popleft()]:
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def test_every_state_has_an_entry() -> None:
    assert set(TRANSITIONS) == set(TaskState)


def test_terminal_states_have_no_exits() -> None:
    for state in TERMINAL:
        assert TRANSITIONS[state] == frozenset()


def test_every_non_terminal_state_can_be_cancelled() -> None:
    for state in set(TaskState) - TERMINAL:
        assert can_transition(state, TaskState.CANCELLED)


def test_every_state_is_reachable_from_pending() -> None:
    assert _reachable_from(TaskState.PENDING) == set(TaskState)


def test_every_non_terminal_state_can_reach_a_terminal_state() -> None:
    for state in set(TaskState) - TERMINAL:
        assert _reachable_from(state) & TERMINAL


def test_happy_path_is_legal() -> None:
    path = [
        TaskState.PENDING,
        TaskState.READY,
        TaskState.LEASED,
        TaskState.RUNNING,
        TaskState.AWAITING_GATE,
        TaskState.INTEGRATING,
        TaskState.DONE,
    ]
    for current, target in itertools.pairwise(path):
        assert can_transition(current, target), f"{current} -> {target}"


def test_skipping_the_gate_is_illegal() -> None:
    assert not can_transition(TaskState.RUNNING, TaskState.DONE)
    assert not can_transition(TaskState.AWAITING_GATE, TaskState.DONE)
