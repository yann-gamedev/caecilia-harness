"""Task graph: validation, ordering, readiness and write-set conflicts (ADR-0013).

Pure logic, no IO. An edge goes from a task to each of its blockers (`blocked_by`): the task may
only start once all of them are DONE.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from caecilia_harness.core.globs import InvalidPatternError, may_overlap, validate_pattern
from caecilia_harness.core.states import TaskState

_ID = re.compile(r"[A-Za-z0-9_-]+")
_FAILED = frozenset({TaskState.FAILED, TaskState.CANCELLED})

IssueKind = Literal[
    "duplicate_id", "missing_blocker", "self_blocker", "cycle", "bad_pattern", "bad_id"
]


@dataclass(frozen=True)
class GraphNode:
    id: str
    blocked_by: tuple[str, ...] = ()
    write_set: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphIssue:
    kind: IssueKind
    task_id: str
    detail: str


class GraphError(ValueError):
    """The graph is invalid; `issues` lists every problem found."""

    def __init__(self, issues: Sequence[GraphIssue]) -> None:
        self.issues = list(issues)
        summary = "; ".join(f"{i.kind} ({i.task_id}): {i.detail}" for i in self.issues)
        super().__init__(f"invalid task graph: {summary}")


def validate_graph(nodes: Sequence[GraphNode]) -> list[GraphIssue]:
    """Return every problem in the graph, in a deterministic order (empty list = valid).

    Per node, in input order: bad_id, duplicate_id, self_blocker, missing_blocker, bad_pattern.
    Cycles follow, ordered by their first task in input order.
    """
    known = {node.id for node in nodes}
    issues: list[GraphIssue] = []
    seen: set[str] = set()
    for node in nodes:
        if not _ID.fullmatch(node.id):
            issues.append(GraphIssue("bad_id", node.id, "id must match [A-Za-z0-9_-]+"))
        if node.id in seen:
            issues.append(GraphIssue("duplicate_id", node.id, "id is used by more than one task"))
        seen.add(node.id)
        for blocker in node.blocked_by:
            if blocker == node.id:
                issues.append(GraphIssue("self_blocker", node.id, "task is blocked by itself"))
            elif blocker not in known:
                issues.append(
                    GraphIssue("missing_blocker", node.id, f"blocker {blocker!r} does not exist")
                )
        for pattern in node.write_set:
            try:
                validate_pattern(pattern)
            except InvalidPatternError as exc:
                issues.append(GraphIssue("bad_pattern", node.id, f"{pattern!r}: {exc}"))
    issues.extend(
        GraphIssue("cycle", cycle[0], " -> ".join(cycle)) for cycle in _find_cycles(nodes)
    )
    return issues


def _find_cycles(nodes: Sequence[GraphNode]) -> list[list[str]]:
    """Cycles as paths `[a, b, ..., a]`, each starting at its earliest task in input order.

    Reports at least one cycle per group of mutually dependent tasks (not every possible one).
    Self-edges and missing blockers are separate issue kinds and are ignored here.
    """
    order: dict[str, int] = {}
    edges: dict[str, tuple[str, ...]] = {}
    for index, node in enumerate(nodes):
        if node.id not in order:  # the first definition of a duplicated id wins
            order[node.id] = index
            edges[node.id] = node.blocked_by
    graph = {
        task: [b for b in blockers if b in order and b != task] for task, blockers in edges.items()
    }

    state: dict[str, int] = {}  # absent = unvisited, 1 = on the current path, 2 = finished
    found: dict[tuple[str, ...], None] = {}  # insertion-ordered set

    def visit(start: str) -> None:
        # Iterative DFS, so a long dependency chain cannot hit the recursion limit.
        path = [start]
        pending = [iter(graph[start])]
        state[start] = 1
        while path:
            blocker = next(pending[-1], None)
            if blocker is None:
                state[path.pop()] = 2
                pending.pop()
            elif state.get(blocker) == 1:
                loop = path[path.index(blocker) :]
                first = min(range(len(loop)), key=lambda i: order[loop[i]])
                found.setdefault(tuple(loop[first:] + loop[:first]), None)
            elif blocker not in state:
                state[blocker] = 1
                path.append(blocker)
                pending.append(iter(graph[blocker]))

    for task in graph:
        if task not in state:
            visit(task)
    return [[*loop, loop[0]] for loop in sorted(found, key=lambda loop: order[loop[0]])]


def topological_order(nodes: Sequence[GraphNode]) -> list[str]:
    """Task ids with every blocker before its dependents.

    Stable: among tasks that could go next, input order decides. Raises `GraphError` if the
    graph is invalid.
    """
    issues = validate_graph(nodes)
    if issues:
        raise GraphError(issues)
    remaining = list(nodes)
    placed: set[str] = set()
    result: list[str] = []
    while remaining:
        # A valid (acyclic) graph always has a task whose blockers are all placed.
        index = next(i for i, node in enumerate(remaining) if placed.issuperset(node.blocked_by))
        node = remaining.pop(index)
        placed.add(node.id)
        result.append(node.id)
    return result


def ready(nodes: Sequence[GraphNode], states: Mapping[str, TaskState]) -> list[str]:
    """PENDING tasks whose blockers are all DONE, in topological order."""
    by_id = {node.id: node for node in nodes}
    return [
        task
        for task in topological_order(nodes)
        if states.get(task) is TaskState.PENDING
        and all(states.get(blocker) is TaskState.DONE for blocker in by_id[task].blocked_by)
    ]


def blocked_by_failure(
    nodes: Sequence[GraphNode], states: Mapping[str, TaskState]
) -> dict[str, str]:
    """PENDING tasks with a direct blocker that is FAILED or CANCELLED -> that blocker's id.

    Only direct blockers are considered; the caller cancels these tasks and calls again, which
    walks a chain one level per round.
    """
    blocked: dict[str, str] = {}
    for node in nodes:
        if states.get(node.id) is not TaskState.PENDING:
            continue
        failed = next((b for b in node.blocked_by if states.get(b) in _FAILED), None)
        if failed is not None:
            blocked[node.id] = failed
    return blocked


def conflicts(
    write_sets: Mapping[str, Sequence[str]], running: Iterable[str], candidate: str
) -> list[str]:
    """Running tasks whose write set may overlap the candidate's (they must not run together)."""
    return [
        task
        for task in running
        if task != candidate and may_overlap(write_sets[task], write_sets[candidate])
    ]
