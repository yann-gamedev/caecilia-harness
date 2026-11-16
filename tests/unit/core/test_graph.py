"""Task graph validation, ordering and readiness (backlog M2-01, ADR-0013)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from caecilia_harness.core.graph import (
    GraphError,
    GraphNode,
    blocked_by_failure,
    conflicts,
    ready,
    topological_order,
    validate_graph,
)
from caecilia_harness.core.states import TaskState

S = TaskState


def n(task_id: str, *blocked_by: str, write_set: tuple[str, ...] = ()) -> GraphNode:
    return GraphNode(id=task_id, blocked_by=tuple(blocked_by), write_set=write_set)


def kinds(nodes: list[GraphNode]) -> list[tuple[str, str]]:
    return [(issue.kind, issue.task_id) for issue in validate_graph(nodes)]


# --- validate_graph --------------------------------------------------------------------------------


def test_valid_graph_has_no_issues() -> None:
    nodes = [n("api", write_set=("api/**",)), n("web", "api"), n("docs", "api", "web")]
    assert validate_graph(nodes) == []
    assert validate_graph([]) == []


def test_duplicate_id() -> None:
    assert kinds([n("a"), n("b"), n("a")]) == [("duplicate_id", "a")]


def test_missing_blocker() -> None:
    [issue] = validate_graph([n("a", "ghost")])
    assert (issue.kind, issue.task_id) == ("missing_blocker", "a")
    assert "ghost" in issue.detail


def test_self_blocker() -> None:
    assert kinds([n("a", "a")]) == [("self_blocker", "a")]


@pytest.mark.parametrize("bad", ["", "a b", "a/b", "../x", "a.b", "tugas#1"])
def test_bad_id(bad: str) -> None:
    assert kinds([n(bad)]) == [("bad_id", bad)]


def test_bad_pattern() -> None:
    [issue] = validate_graph([n("a", write_set=("ok/**", "../escape"))])
    assert (issue.kind, issue.task_id) == ("bad_pattern", "a")
    assert "../escape" in issue.detail


def test_cycle_reports_its_path() -> None:
    [issue] = validate_graph([n("a", "c"), n("b", "a"), n("c", "b")])
    assert issue.kind == "cycle"
    assert issue.task_id == "a"
    assert issue.detail == "a -> c -> b -> a"


def test_two_node_cycle_and_unrelated_valid_part() -> None:
    issues = validate_graph([n("ok"), n("x", "y"), n("y", "x"), n("z", "ok")])
    assert [(i.kind, i.detail) for i in issues] == [("cycle", "x -> y -> x")]


def test_two_separate_cycles_are_both_reported() -> None:
    issues = validate_graph([n("a", "b"), n("b", "a"), n("c", "d"), n("d", "c")])
    assert [i.detail for i in issues] == ["a -> b -> a", "c -> d -> c"]


def test_all_problems_are_reported_in_a_stable_order() -> None:
    nodes = [
        n("a", "ghost", write_set=("/abs",)),
        n("b", "b"),
        n("a"),
        n("c", "d"),
        n("d", "c"),
        n("bad id"),
    ]
    expected = [
        ("missing_blocker", "a"),
        ("bad_pattern", "a"),
        ("self_blocker", "b"),
        ("duplicate_id", "a"),
        ("bad_id", "bad id"),
        ("cycle", "c"),
    ]
    assert kinds(nodes) == expected
    assert kinds(nodes) == expected  # deterministic


# --- topological_order -----------------------------------------------------------------------------


def test_topological_order_is_stable_and_respects_edges() -> None:
    nodes = [n("d", "b", "c"), n("b", "a"), n("c", "a"), n("a"), n("e")]
    assert topological_order(nodes) == ["a", "b", "c", "d", "e"]
    # Already ordered input is returned as given.
    ordered = [n("a"), n("b", "a"), n("c", "a"), n("d", "b", "c")]
    assert topological_order(ordered) == ["a", "b", "c", "d"]
    # Independent tasks keep their input order.
    assert topological_order([n("z"), n("m"), n("a")]) == ["z", "m", "a"]


def test_topological_order_refuses_invalid_graph() -> None:
    with pytest.raises(GraphError) as info:
        topological_order([n("a", "b"), n("b", "a")])
    assert [issue.kind for issue in info.value.issues] == ["cycle"]


@settings(max_examples=200, deadline=None)
@given(st.data())
def test_topological_order_property(data: st.DataObject) -> None:
    size = data.draw(st.integers(min_value=0, max_value=8))
    ids = [f"t{i}" for i in range(size)]
    # Edges only point to earlier ids, so the graph is acyclic by construction...
    nodes = [
        n(
            task_id,
            *data.draw(st.lists(st.sampled_from(ids[:i]), unique=True) if i else st.just([])),
        )
        for i, task_id in enumerate(ids)
    ]
    # ...then shuffled, so input order says nothing about dependency order.
    shuffled = data.draw(st.permutations(nodes))
    order = topological_order(shuffled)
    assert sorted(order) == sorted(ids)
    position = {task_id: i for i, task_id in enumerate(order)}
    for node in shuffled:
        assert all(position[blocker] < position[node.id] for blocker in node.blocked_by)


# --- ready / blocked_by_failure / conflicts -----------------------------------------------------------


def test_ready_lists_pending_tasks_whose_blockers_are_done() -> None:
    nodes = [n("d", "b", "c"), n("c", "a"), n("b", "a"), n("a"), n("e"), n("f", "e")]
    states = {
        "a": S.DONE,
        "b": S.DONE,
        "c": S.PENDING,  # blocker a is DONE -> ready
        "d": S.PENDING,  # c is not DONE yet
        "e": S.PENDING,  # no blockers -> ready
        "f": S.PENDING,  # e not DONE
    }
    assert ready(nodes, states) == ["c", "e"]
    assert ready(nodes, dict.fromkeys(states, S.DONE)) == []
    assert ready(nodes, dict.fromkeys(states, S.PENDING)) == ["a", "e"]


def test_ready_ignores_non_pending_tasks() -> None:
    nodes = [n("a"), n("b", "a")]
    assert ready(nodes, {"a": S.RUNNING, "b": S.PENDING}) == []
    assert ready(nodes, {"a": S.DONE, "b": S.READY}) == []
    assert ready(nodes, {"a": S.NEEDS_HUMAN, "b": S.PENDING}) == []  # stays PENDING, not ready


def test_blocked_by_failure_only_looks_at_direct_blockers() -> None:
    nodes = [n("a"), n("b", "a"), n("c", "b"), n("d"), n("e", "d", "a"), n("f", "d")]
    states = {
        "a": S.FAILED,
        "b": S.PENDING,
        "c": S.PENDING,  # blocked only transitively: the caller handles it on the next round
        "d": S.CANCELLED,
        "e": S.PENDING,
        "f": S.RUNNING,  # not PENDING -> not reported
    }
    assert blocked_by_failure(nodes, states) == {"b": "a", "e": "d"}
    # Second round, after the caller cancelled b:
    states["b"] = S.CANCELLED
    assert blocked_by_failure(nodes, states) == {"c": "b", "e": "d"}
    assert blocked_by_failure(nodes, dict.fromkeys(states, S.PENDING)) == {}


def test_conflicts_lists_running_tasks_with_overlapping_write_sets() -> None:
    write_sets = {
        "api": ["api/**"],
        "web": ["web/**"],
        "all": [],
        "api2": ["api/v2/**"],
    }
    assert conflicts(write_sets, ["api", "web"], "api2") == ["api"]
    assert conflicts(write_sets, ["api", "web"], "all") == ["api", "web"]
    assert conflicts(write_sets, ["web"], "api") == []
    assert conflicts(write_sets, [], "all") == []
    assert conflicts(write_sets, ["api"], "api") == []  # a task does not conflict with itself
