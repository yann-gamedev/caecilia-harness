"""The run loop: drives a task from PENDING to a terminal state (or NEEDS_HUMAN).

Every state change is an event appended with a per-stream CAS (`expect`). Side effects that must
not be repeated (spawning a worker) go through the transactional outbox and re-check the task
state before acting, so processing an outbox item twice is harmless. A task is only run under a
lease held by this process (`holder`); after a crash `recover()` brings every task back to a
consistent state before `resume()` continues (docs/architecture.md, M1-11).

Depends only on `core` (ports, models, states); concrete adapters are wired in `bootstrap.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from caecilia_harness.core.clock import Clock, SystemClock
from caecilia_harness.core.globs import InvalidPatternError, matches, normalize_write_set
from caecilia_harness.core.graph import (
    GraphNode,
    blocked_by_failure,
    conflicts,
    ready,
    topological_order,
    validate_graph,
)
from caecilia_harness.core.models import (
    Action,
    Actor,
    AddTask,
    Budget,
    CancelTask,
    ContextPack,
    DiffArtifact,
    Event,
    GateReport,
    GraphOp,
    Lease,
    NewEvent,
    OutboxItem,
    RetryTask,
    RunHandle,
    SetBlockers,
    TaskRow,
    TaskSpec,
    TaskStateChange,
    WorkerResult,
    Workspace,
)
from caecilia_harness.core.ports import (
    Gate,
    StateStore,
    UserPrompter,
    WorkerAdapter,
    WorkspaceBackend,
)
from caecilia_harness.core.states import TERMINAL, TaskState

S = TaskState
SPAWN_WORKER = "spawn_worker"
#: States where `run()` stops driving a task: terminal, or waiting for a person.
STOP_STATES = TERMINAL | {S.NEEDS_HUMAN}
DEFAULT_LEASE_TTL_S = 30.0
#: Decision D11: until the M2-B benchmark says otherwise.
DEFAULT_MAX_PARALLEL = 2
#: States that count against `max_parallel` (a task waiting for the gate does not, ADR-0013).
_HOLDS_WORKER_SLOT = frozenset({S.LEASED, S.RUNNING})
#: States whose write set blocks a conflicting task from being leased.
_BUSY = frozenset({S.LEASED, S.RUNNING, S.AWAITING_GATE, S.INTEGRATING})
#: States the scheduler handles itself (PENDING, READY) or that need nothing more.
_NO_DRIVER = STOP_STATES | {S.PENDING, S.READY}

#: hash -> (path, size in bytes); provided by bootstrap so core never touches the filesystem.
ArtifactInfo = Callable[[str], tuple[str, int]]


HOLD_ENV = "CAECILIA_TEST_HOLD_AT"
_HOLD_SECONDS = 60


def _chaos_hold(point: str, task_id: str | None = None) -> None:
    """Test hook for the chaos suite only (tests/chaos): never set this variable otherwise.

    CAECILIA_TEST_HOLD_AT=<point> holds at that point for any task; <point>@<plan id> only for
    that task (plan id = the task id after `<run_id>-`). The hook announces itself on stdout
    and blocks the whole process, so the test can kill it at an exact, already-committed
    moment. A no-op without the variable.
    """
    wanted = os.environ.get(HOLD_ENV)
    if not wanted:
        return
    want_point, _, want_task = wanted.partition("@")
    if want_point != point:
        return
    # Run ids never contain '-', so the plan id is everything after the first one.
    if want_task and (task_id is None or task_id.partition("-")[2] != want_task):
        return
    print(f"CAECILIA_HOLD {wanted}", flush=True)
    time.sleep(_HOLD_SECONDS)


class OrchestratorError(Exception):
    """The orchestrator cannot make progress (e.g. a RUNNING task without a live worker)."""


class GraphRevisionError(OrchestratorError):
    """A graph revision was refused; `problems` lists every reason. Nothing was written."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("graph revision refused:" + "".join(f"\n- {p}" for p in self.problems))


class GraphRevisionConflictError(OrchestratorError):
    """The graph changed since the caller read it (compare-and-set on the graph stream)."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"graph revision is {actual}, not {expected}: read the graph again and retry"
        )


#: A task in these states is being worked on or merged right now; cancelling it from a
#: revision would race the worker or the gate, so it is refused.
_IN_PROGRESS = frozenset({S.LEASED, S.RUNNING, S.INTEGRATING})


@dataclass
class _Live:
    worker: WorkerAdapter
    handle: RunHandle


def _nodes(specs: Iterable[TaskSpec]) -> list[GraphNode]:
    return [
        GraphNode(id=spec.id, blocked_by=tuple(spec.blocked_by), write_set=tuple(spec.write_set))
        for spec in specs
    ]


class _GraphDraft:
    """An in-memory copy of a run's tasks that revision operations are applied to.

    Problems are collected (not raised) so a refused revision reports all of them. State
    changes carry the right from-state and revision even when one task is touched twice.
    """

    def __init__(self, run_id: str, tasks: Sequence[TaskRow], default_worker: str) -> None:
        self.run_id = run_id
        self.default_worker = default_worker
        self.specs: dict[str, TaskSpec] = {t.id: t.spec for t in tasks}
        self.states: dict[str, TaskState] = {t.id: t.state for t in tasks}
        self.revisions: dict[str, int] = {t.id: t.revision for t in tasks}
        self.problems: list[str] = []
        self.added: set[str] = set()
        self.touched: set[str] = set()
        #: (task id, event type, payload) for every event except task.created.
        self.changes: list[tuple[str, str, dict[str, Any]]] = []

    def full(self, plan_id: str) -> str:
        return f"{self.run_id}-{plan_id}"

    def apply(self, index: int, op: GraphOp) -> None:
        where = f"op {index} ({op.op})"
        if isinstance(op, AddTask):
            self._add(where, op)
            return
        task_id = self.full(op.task_id)
        if task_id not in self.specs:
            self.problems.append(f"{where}: task {op.task_id!r} does not exist in this run")
            return
        state = self.states[task_id]
        if isinstance(op, SetBlockers):
            if state is not S.PENDING:
                self.problems.append(
                    f"{where}: task {op.task_id!r} is {state.value}; blockers can only change "
                    "while it is PENDING"
                )
                return
            blockers = [self.full(b) for b in dict.fromkeys(op.blocked_by)]
            self.specs[task_id] = self.specs[task_id].model_copy(update={"blocked_by": blockers})
            if task_id not in self.added:  # an added task is simply created with its final spec
                payload = {"task_id": task_id, "blocked_by": blockers}
                self.changes.append((task_id, "task.blockers_changed", payload))
        elif isinstance(op, CancelTask):
            if state in TERMINAL or state in _IN_PROGRESS:
                self.problems.append(
                    f"{where}: task {op.task_id!r} is {state.value}; only tasks that are not "
                    "finished and not leased, running or integrating can be cancelled"
                )
                return
            self._change_state(task_id, S.CANCELLED, "dibatalkan lewat revisi graf")
        elif isinstance(op, RetryTask):
            if state is not S.NEEDS_HUMAN:
                self.problems.append(
                    f"{where}: task {op.task_id!r} is {state.value}; only NEEDS_HUMAN tasks "
                    "can be retried"
                )
                return
            self._change_state(task_id, S.READY, "dibereskan manusia")
        self.touched.add(task_id)

    def _add(self, where: str, op: AddTask) -> None:
        task = op.task
        task_id = self.full(task.id)
        if task_id in self.specs:
            self.problems.append(f"{where}: task {task.id!r} already exists in this run")
            return
        try:
            write_set = normalize_write_set(task.write_set)
        except InvalidPatternError:
            write_set = list(task.write_set)  # reported by graph validation as bad_pattern
        blockers = dict.fromkeys([*task.blocked_by, *op.blocked_by])
        self.specs[task_id] = TaskSpec(
            id=task_id,
            run_id=self.run_id,
            title=task.title,
            instructions=task.instructions,
            acceptance=list(task.acceptance),
            write_set=write_set,
            blocked_by=[self.full(b) for b in blockers],
            idempotent=task.idempotent,
            worker=task.worker or self.default_worker,
            budget=task.budget or Budget(),
        )
        self.states[task_id] = S.PENDING
        self.revisions[task_id] = 1
        self.added.add(task_id)
        self.touched.add(task_id)

    def _change_state(self, task_id: str, to: TaskState, reason: str) -> None:
        change = TaskStateChange(
            task_id=task_id,
            from_state=self.states[task_id],
            to_state=to,
            revision=self.revisions[task_id] + 1,
            reason=reason,
        )
        self.changes.append((task_id, "task.state_changed", change.model_dump(mode="json")))
        self.states[task_id] = to
        self.revisions[task_id] += 1


def _is_transient(error: str | None) -> bool:
    # Convention for worker errors: "transient..." may succeed if retried; everything else
    # (budget_exceeded:*, provider_error:*, worker_crashed:*, permanent) is final.
    return error is not None and error.startswith("transient")


class Orchestrator:
    def __init__(
        self,
        store: StateStore,
        workspace: WorkspaceBackend,
        gate: Gate,
        workers: Mapping[str, WorkerAdapter],
        prompter: UserPrompter,
        max_attempts: int = 3,
        artifact_info: ArtifactInfo | None = None,
        closers: Sequence[Callable[[], None]] = (),
        clock: Clock | None = None,
        lease_ttl_s: float = DEFAULT_LEASE_TTL_S,
    ) -> None:
        self.store = store
        self.workspace = workspace
        self.gate = gate
        self.workers = dict(workers)
        self.prompter = prompter
        self.max_attempts = max_attempts
        self.artifact_info = artifact_info
        self.clock: Clock = clock or SystemClock()
        self.lease_ttl_s = lease_ttl_s
        #: New per orchestrator start; leases from any other boot belong to a dead process.
        self.boot_id = uuid.uuid4().hex
        self.holder = f"{socket.gethostname()}:{os.getpid()}:{self.boot_id}"
        self.actor = Actor(kind="orchestrator", id=self.boot_id)
        self._closers = list(closers)
        self._stream_seq: dict[str, int] = {}
        self._live: dict[str, _Live] = {}
        self._workspaces: dict[str, Workspace] = {}
        self._outbox_in_flight: set[int] = set()
        self._merge_slot = asyncio.Lock()
        self._worker_slot_freed = asyncio.Event()
        self._run_usage: dict[str, dict[str, int]] = {}
        self._budget_stopped: set[str] = set()

    def close(self) -> None:
        """Release resources handed over by bootstrap (DB connection, writer lock)."""
        for close in self._closers:
            close()
        self._closers.clear()

    # --- public API ----------------------------------------------------------------------------

    def create_run(
        self,
        goal: str,
        repo_path: str,
        base_commit: str,
        specs: Sequence[TaskSpec],
        target_branch: str = "main",
        options: Mapping[str, Any] | None = None,
        *,
        max_parallel: int | None = None,
        budget: Budget | None = None,
    ) -> str:
        """Record a new run with its tasks and graph revision 1, in one append. Returns the run id.

        The graph is validated first (`GraphError` on any problem, nothing is written). Tasks are
        created in topological order, so every blocker exists before its dependents. `options`
        (check command, worker kinds, ...) are stored with the run so `resume` can rebuild the
        same orchestrator.
        """
        if not specs:
            raise ValueError("a run needs at least one task")
        run_id = specs[0].run_id
        if any(spec.run_id != run_id for spec in specs):
            raise ValueError("all tasks of a run must share the same run_id")
        by_id = {spec.id: spec for spec in specs}
        ordered = [by_id[task_id] for task_id in topological_order(_nodes(specs))]

        run_created = self._event(
            f"run:{run_id}",
            "run.created",
            {
                "run_id": run_id,
                "goal": goal,
                "repo_path": repo_path,
                "base_commit": base_commit,
                "target_branch": target_branch,
                "max_parallel": max_parallel,
                "budget": budget.model_dump() if budget is not None else {},
                "options": dict(options or {}),
            },
        )
        tasks_created = [
            self._event(f"task:{spec.id}", "task.created", {"spec": spec.model_dump(mode="json")})
            for spec in ordered
        ]
        graph_revised = self._event(
            f"graph:{run_id}",
            "graph.revised",
            {
                "revision": 1,
                "tasks": [spec.id for spec in ordered],
                "edges": [[spec.id, blocker] for spec in ordered for blocker in spec.blocked_by],
            },
        )
        expect = {f"run:{run_id}": 0, f"graph:{run_id}": 0}
        expect |= {f"task:{spec.id}": 0 for spec in ordered}
        self.store.append([run_created, *tasks_created, graph_revised], expect=expect)
        return run_id

    async def run(self, run_id: str) -> str:
        """Drive the run's tasks concurrently until none can move (ADR-0013, scheduler).

        One scheduler loop plus one driver coroutine per active task. Each round the loop
        promotes or cancels PENDING tasks, gives every task already in flight (e.g. after
        recovery) a driver, and leases READY tasks in topological order while fewer than
        `max_parallel` are LEASED/RUNNING and the write set conflicts with no busy task. It then
        waits for any driver to finish or release a worker slot. If a driver fails unexpectedly,
        every other driver and
        worker is cancelled and the error is raised; `resume` tidies up from the event log.
        Records `run.finished` and returns the run status.
        """
        tasks = self.store.list_tasks(run_id)
        order = topological_order(_nodes(t.spec for t in tasks))
        write_sets = {t.id: list(t.spec.write_set) for t in tasks}
        limit = self._max_parallel(run_id)
        self._restore_run_usage(run_id)
        drivers: dict[str, asyncio.Task[None]] = {}
        try:
            while True:
                self._worker_slot_freed.clear()
                self._schedule(run_id, order, write_sets, limit, drivers)
                if not drivers:
                    break
                # Drivers may wait at the gate indefinitely after releasing their worker slot.
                slot_freed = asyncio.create_task(self._worker_slot_freed.wait())
                try:
                    finished, _ = await asyncio.wait(
                        [*drivers.values(), slot_freed], return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    slot_freed.cancel()
                    await asyncio.gather(slot_freed, return_exceptions=True)
                for task_id, driver in list(drivers.items()):
                    if driver in finished:
                        del drivers[task_id]
                        driver.result()  # re-raises a driver's unexpected error
        except BaseException:
            await self._abort(drivers)
            raise
        status = self._run_status(run_id)
        payload: dict[str, Any] = {"run_id": run_id, "status": status}
        if run_id in self._budget_stopped:
            payload["reason"] = "run token budget exceeded"
        self.store.append([self._event(f"run:{run_id}", "run.finished", payload)])
        return status

    def _restore_run_usage(self, run_id: str) -> None:
        """Rebuild token totals from durable usage, including attempts before this process."""
        used = {"input_tokens": 0, "output_tokens": 0}
        for task in self.store.list_tasks(run_id):
            for event in self.store.read_stream(f"task:{task.id}"):
                if event.type == "usage.recorded":
                    for name in used:
                        used[name] += int(event.payload.get(name, 0))
        self._run_usage[run_id] = used
        if any(e.type == "run.budget_exceeded" for e in self.store.read_stream(f"run:{run_id}")):
            self._budget_stopped.add(run_id)
        self._check_run_budget(run_id)

    def _check_run_budget(self, run_id: str) -> None:
        used = self._run_usage[run_id]
        limit = Budget.model_validate(self._run_info(run_id).get("budget") or {})
        if run_id in self._budget_stopped or not any(
            maximum is not None and used[name] > maximum
            for name, maximum in (
                ("input_tokens", limit.max_input_tokens),
                ("output_tokens", limit.max_output_tokens),
            )
        ):
            return
        self.store.append(
            [
                self._event(
                    f"run:{run_id}",
                    "run.budget_exceeded",
                    {"used": dict(used), "limit": limit.model_dump()},
                    idempotency_key=f"{run_id}:budget_exceeded",
                )
            ]
        )
        self._budget_stopped.add(run_id)
        _chaos_hold("BUDGET_EXCEEDED")

    def _max_parallel(self, run_id: str) -> int:
        """`max_parallel` recorded with the run (CLI option or plan), else the default (D11)."""
        value = self._run_info(run_id).get("max_parallel")
        return max(1, int(value)) if value else DEFAULT_MAX_PARALLEL

    def _schedule(
        self,
        run_id: str,
        order: Sequence[str],
        write_sets: Mapping[str, Sequence[str]],
        limit: int,
        drivers: dict[str, asyncio.Task[None]],
    ) -> None:
        """One scheduling round. Synchronous on purpose: no other coroutine runs meanwhile."""
        for task_id in order:
            task = self.store.get_task(task_id)
            if task.state is S.PENDING:
                self._promote_or_cancel(task)
        states = {task.id: task.state for task in self.store.list_tasks(run_id)}

        # Tasks already in flight (found after recovery, or REWORK/RETRY_WAIT on their way
        # back to READY) get a driver too.
        for task_id in order:
            if task_id not in drivers and states[task_id] not in _NO_DRIVER:
                drivers[task_id] = self._start_driver(task_id)

        if run_id in self._budget_stopped:
            return  # existing drivers finish, but no new task receives a lease
        busy = [task_id for task_id in order if states[task_id] in _BUSY]
        running = sum(states[task_id] in _HOLDS_WORKER_SLOT for task_id in order)
        for task_id in order:
            if states[task_id] is not S.READY or task_id in drivers:
                continue
            if running >= limit:
                break
            if conflicts(write_sets, busy, task_id):
                continue  # may touch the same files as a busy task: wait for it
            self._lease_and_spawn(self.store.get_task(task_id))
            running += 1
            busy.append(task_id)
            drivers[task_id] = self._start_driver(task_id)

    def _start_driver(self, task_id: str) -> asyncio.Task[None]:
        return asyncio.create_task(self._drive(task_id), name=f"caecilia-driver-{task_id}")

    async def _drive(self, task_id: str) -> None:
        """Advance one task until it stops, or is back to READY (retry/rework): the scheduler
        then decides when it may run again."""
        while True:
            state = await self.advance(task_id)
            if state in STOP_STATES or state is S.READY:
                return

    async def _abort(self, drivers: Mapping[str, asyncio.Task[None]]) -> None:
        """Stop everything this run started, so no worker or coroutine outlives `run()`."""
        # Stop spawns first: no driver may register another worker during cancellation.
        for driver in drivers.values():
            driver.cancel()
        await asyncio.gather(*drivers.values(), return_exceptions=True)
        for live in list(self._live.values()):
            # Cleanup must reach every worker and preserve the original driver error.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await live.worker.cancel(live.handle, "hard")
        for live in list(self._live.values()):
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await live.worker.result(live.handle)

    def _run_status(self, run_id: str) -> str:
        if run_id in self._budget_stopped:
            return "needs_human"
        states = {task.state for task in self.store.list_tasks(run_id)}
        if states == {S.DONE}:
            return "done"
        # A task left PENDING here is waiting on a NEEDS_HUMAN blocker.
        if S.NEEDS_HUMAN in states or S.PENDING in states:
            return "needs_human"
        if S.FAILED in states:
            return "failed"
        return "cancelled"

    # --- graph revisions (ADR-0003) ---------------------------------------------------------

    def graph_revision(self, run_id: str) -> int:
        """Revision of the run's graph: that of the last `graph.revised` (0 if none)."""
        revisions = [
            e for e in self.store.read_stream(f"graph:{run_id}") if e.type == "graph.revised"
        ]
        return int(revisions[-1].payload["revision"]) if revisions else 0

    def revise_graph(self, run_id: str, ops: Sequence[GraphOp], expected_revision: int) -> int:
        """Apply `ops` to a stopped run's graph as one new revision. Returns the new revision.

        Compare-and-set: `expected_revision` must be the current one, else
        `GraphRevisionConflictError`. All operations are applied to an in-memory copy and the
        whole resulting graph is validated; any problem raises `GraphRevisionError` listing all
        of them and nothing is written. Otherwise one append (CAS on the graph stream and every
        touched task stream) records the operations' events, `graph.revised`, and
        `run.reopened` if the run had finished. Task ids in `ops` are plan ids.

        Only while the run is not executing (ADR-0002/0013): the caller holds the writer lock.
        """
        current = self.graph_revision(run_id)
        if current != expected_revision:
            raise GraphRevisionConflictError(expected_revision, current)
        draft = _GraphDraft(run_id, self.store.list_tasks(run_id), self._default_worker(run_id))
        for index, op in enumerate(ops, start=1):
            draft.apply(index, op)
        nodes = _nodes(draft.specs.values())
        problems = draft.problems + [
            f"task {i.task_id}: {i.kind}: {i.detail}" for i in validate_graph(nodes)
        ]
        if problems:
            raise GraphRevisionError(problems)

        order = topological_order(nodes)
        created = [
            self._event(
                f"task:{task_id}",
                "task.created",
                {"spec": draft.specs[task_id].model_dump(mode="json")},
            )
            for task_id in order
            if task_id in draft.added  # blockers first: task_deps has foreign keys
        ]
        changes = [
            self._event(f"task:{task_id}", type_, payload)
            for task_id, type_, payload in draft.changes
        ]
        revised = self._event(
            f"graph:{run_id}",
            "graph.revised",
            {
                "revision": current + 1,
                "tasks": order,
                "edges": [[t, b] for t in order for b in draft.specs[t].blocked_by],
                "ops": [op.model_dump(mode="json") for op in ops],
            },
        )
        events = [*created, *changes, revised]
        streams = [f"graph:{run_id}", *(f"task:{task_id}" for task_id in sorted(draft.touched))]
        if self._run_finished(run_id):
            events.append(self._event(f"run:{run_id}", "run.reopened", {"run_id": run_id}))
            streams.append(f"run:{run_id}")
        expect = {stream: self._last_seq(stream) for stream in streams}
        self.store.append(events, expect=expect)
        for stream in streams:
            self._stream_seq.pop(stream, None)  # read again on the next append
        return current + 1

    def _default_worker(self, run_id: str) -> str:
        options = self._run_info(run_id).get("options") or {}
        if options.get("worker_kind"):
            return str(options["worker_kind"])
        tasks = self.store.list_tasks(run_id)
        return tasks[0].spec.worker if tasks else "native"

    def _run_finished(self, run_id: str) -> bool:
        """True if the run's last finish is newer than its last reopening."""
        closing = [
            e.type
            for e in self.store.read_stream(f"run:{run_id}")
            if e.type in ("run.finished", "run.reopened")
        ]
        return bool(closing) and closing[-1] == "run.finished"

    def _last_seq(self, stream: str) -> int:
        events = self.store.read_stream(stream)
        return events[-1].stream_seq if events else 0

    async def resume(self, run_id: str) -> str:
        """Recover after a crash, then continue the run from its current state."""
        await self.recover()
        return await self.run(run_id)

    async def advance(self, task_id: str) -> TaskState:
        """Perform exactly one step for the task and return its new state."""
        task = self.store.get_task(task_id)
        match task.state:
            case S.PENDING:
                self._promote_or_cancel(task)
            case S.READY:
                self._lease_and_spawn(task)
            case S.LEASED:
                await self.process_outbox(task_id)
                if self.store.get_task(task_id).state is S.LEASED:
                    raise OrchestratorError(f"task {task_id} is LEASED but nothing spawned it")
            case S.RUNNING:
                await self._collect_worker(task)
            case S.RETRY_WAIT:
                self._transition(task, S.READY, "retrying")  # M1: no backoff delay
            case S.AWAITING_GATE | S.INTEGRATING:
                # INTEGRATING is only seen here after recovery: the gate is simply run again.
                await self._integrate(task)
            case S.REWORK:
                self._transition(task, S.READY, "rework with gate report")
            case state if state in STOP_STATES:
                pass
            case state:
                raise OrchestratorError(f"task {task_id}: no M1 handling for state {state}")
        return self.store.get_task(task_id).state

    async def cancel_all(self, mode: Literal["soft", "hard"]) -> None:
        """Cancel every worker this process is running (Ctrl+C in the CLI)."""
        for live in list(self._live.values()):
            await live.worker.cancel(live.handle, mode)

    async def process_outbox(self, task_id: str | None = None) -> None:
        """Perform pending side effects (only `task_id`'s, if given). Safe to call again.

        Several drivers share one event loop, and `worker.start` is awaited while the task is
        still LEASED: two callers could both pass `_spawn`'s state check and start the worker
        twice. Items being processed right now are therefore claimed in memory and skipped by
        everyone else. After a crash that memory is gone, and `_spawn`'s check covers replays.
        """
        for item in self.store.pending_outbox():
            if task_id is not None and item.payload.get("task_id") != task_id:
                continue
            if item.id in self._outbox_in_flight:
                continue
            self._outbox_in_flight.add(item.id)
            try:
                if item.kind != SPAWN_WORKER:
                    self.store.mark_outbox(item.id, "failed")
                    continue
                try:
                    await self._spawn(
                        str(item.payload["task_id"]),
                        int(item.payload["attempt"]),
                        int(item.payload.get("revision", -1)),
                    )
                except Exception:
                    self.store.mark_outbox(item.id, "failed")
                    raise
                self.store.mark_outbox(item.id, "done")
            finally:
                self._outbox_in_flight.discard(item.id)

    async def approve(self, action: Action) -> bool:
        """Ask the person about `action`, recording the request and the decision as events."""
        stream = f"approval:{action.task_id or 'run'}"
        [requested] = self.store.append(
            [self._event(stream, "approval.requested", {"action": action.model_dump(mode="json")})]
        )
        allowed = await self.prompter.ask_approval(action)
        self.store.append(
            [
                self._event(
                    stream,
                    "approval.decided",
                    {"allowed": allowed},
                    causation_id=requested.event_id,
                )
            ]
        )
        return allowed

    # --- recovery ------------------------------------------------------------------------------

    async def recover(self) -> None:
        """Bring tasks left by a crashed orchestrator back to a consistent state.

        Called once at start, before `run`/`resume`. Only leases held by this very process are
        trusted; any other (older boot, or expired) means its worker is gone.
        """
        leases = {lease.task_id: lease for lease in self.store.list_leases()}
        for task in self.store.list_tasks(None):
            if task.state in (S.LEASED, S.RUNNING):
                lease = leases.get(task.id)
                if lease is None or not self._is_own_live(lease):
                    self._recover_orphan(task, lease)
            elif task.state is S.INTEGRATING:
                await self._recover_integrating(task)
            # AWAITING_GATE, READY, PENDING, RETRY_WAIT, REWORK: resume picks them up as-is.

    def _is_own_live(self, lease: Lease) -> bool:
        return lease.holder == self.holder and lease.expires_at > self.clock.now()

    def _recover_orphan(self, task: TaskRow, lease: Lease | None) -> None:
        for item in self.store.pending_outbox():
            if item.kind == SPAWN_WORKER and item.payload.get("task_id") == task.id:
                self.store.mark_outbox(item.id, "cancelled")
        kept = self._workspace_for(task.id)
        expired = self._event(
            f"task:{task.id}",
            "task.lease_expired",
            {
                "task_id": task.id,
                "holder": lease.holder if lease else None,
                # The dead attempt's worktree stays on disk for inspection (keep=True).
                "kept_workspace": kept.path if kept else None,
            },
        )
        self._workspaces.pop(task.id, None)
        task = self._transition(task, S.RECOVERING, "lease lost after crash", before=[expired])
        if task.spec.idempotent and task.attempts < self.max_attempts:
            self._transition(task, S.READY, "idempotent task, safe to run again")
        else:
            reason = (
                "non-idempotent task interrupted: side effects unknown"
                if not task.spec.idempotent
                else "attempts exhausted"
            )
            self._transition(task, S.NEEDS_HUMAN, reason)

    async def _recover_integrating(self, task: TaskRow) -> None:
        merging = self._merging_since_last_gate_start(task.id)
        if merging is None:
            return  # crashed before the merge started: resume runs the gate again
        run = self._run_info(task.run_id)
        head = await self.gate.branch_head(str(run["target_branch"]))
        if head == merging["new"]:
            passed = self._event(
                f"task:{task.id}",
                "gate.passed",
                {"report": None, "recovered": True, "merged_commit": merging["new"]},
            )
            self._transition(task, S.DONE, "merge had landed before the crash", before=[passed])
            await self._discard_workspace(task.id)
        elif head == merging["old"]:
            return  # merge never happened: resume runs the gate again
        else:
            self._transition(
                task,
                S.NEEDS_HUMAN,
                f"target moved to {head} during an interrupted merge "
                f"({merging['old']} -> {merging['new']})",
            )

    def _merging_since_last_gate_start(self, task_id: str) -> dict[str, Any] | None:
        merging: dict[str, Any] | None = None
        for event in self.store.read_stream(f"task:{task_id}"):
            if event.type == "gate.started":
                merging = None
            elif event.type == "gate.merging":
                merging = event.payload
        return merging

    # --- steps ---------------------------------------------------------------------------------

    def _promote_or_cancel(self, task: TaskRow) -> None:
        """PENDING -> READY once every blocker is DONE; -> CANCELLED if a blocker failed.

        A blocker in any other state (running, NEEDS_HUMAN, ...) leaves the task PENDING.
        """
        tasks = self.store.list_tasks(task.run_id)
        nodes = _nodes(t.spec for t in tasks)
        states = {t.id: t.state for t in tasks}
        failed = blocked_by_failure(nodes, states).get(task.id)
        if failed is not None:
            self._transition(task, S.CANCELLED, f"blocker {failed} {states[failed].value}")
        elif task.id in ready(nodes, states):
            self._transition(
                task, S.READY, "blockers done" if task.spec.blocked_by else "no blockers"
            )

    def _lease_and_spawn(self, task: TaskRow) -> None:
        if task.run_id not in self._run_usage:
            self._restore_run_usage(task.run_id)
        if task.run_id in self._budget_stopped:
            return
        attempt = task.attempts + 1
        revision = task.revision + 1  # the LEASED revision: unique per lease, even on retries
        spawn = OutboxItem(
            idempotency_key=f"{task.id}:{attempt}:r{revision}:{SPAWN_WORKER}",
            kind=SPAWN_WORKER,
            payload={"task_id": task.id, "attempt": attempt, "revision": revision},
        )
        acquired = self._event(
            f"task:{task.id}",
            "task.lease_acquired",
            {
                "task_id": task.id,
                "holder": self.holder,
                "attempt": attempt,
                "pid": os.getpid(),
                "expires_at": self._expiry().isoformat(),
            },
        )
        self._transition(task, S.LEASED, f"attempt {attempt}", after=[acquired], outbox=[spawn])

    async def _spawn(self, task_id: str, attempt: int, revision: int) -> None:
        task = self.store.get_task(task_id)
        # Re-check before acting: the item may be replayed after it already took effect.
        if task.state is not S.LEASED or task.attempts + 1 != attempt:
            return
        if revision >= 0 and task.revision != revision:
            return
        worker = self.workers.get(task.spec.worker)
        if worker is None:
            self._give_up_spawn(task, f"no worker named {task.spec.worker!r}")
            return
        run = self._run_info(task.run_id)
        # Later attempts get their own worktree: an earlier one may be kept after a crash.
        workspace_id = task.id if attempt == 1 else f"{task.id}-{attempt}"
        try:
            # ADR-0013: the base is the target's head *now*, so a task sees its blockers' merges.
            base = await self.gate.branch_head(str(run["target_branch"]))
            ws = await self.workspace.create(workspace_id, base)
        except Exception as exc:
            self._give_up_spawn(task, f"workspace creation failed: {exc}")
            return
        self._workspaces[task.id] = ws
        ctx = ContextPack(task=task.spec, handoff_notes=self._handoff_notes(task.id))
        handle = await worker.start(task.spec, ctx, ws, task.spec.budget)
        self._live[task.id] = _Live(worker, handle)
        started = self._event(
            f"task:{task.id}",
            "worker.started",
            {
                "worker": handle.worker,
                "attempt": attempt,
                "workspace_id": ws.task_id,
                "path": ws.path,
                "branch": ws.branch,
                "base_commit": ws.base_commit,
            },
        )
        self._transition(task, S.RUNNING, f"worker {handle.worker} started", before=[started])

    def _give_up_spawn(self, task: TaskRow, reason: str) -> None:
        # LEASED cannot go to NEEDS_HUMAN directly; RECOVERING is the sanctioned path.
        task = self._transition(task, S.RECOVERING, reason, after=[self._released(task.id)])
        self._transition(task, S.NEEDS_HUMAN, reason)

    async def _collect_worker(self, task: TaskRow) -> None:
        live = self._live.get(task.id)
        if live is None:
            raise OrchestratorError(
                f"task {task.id} is RUNNING without a live worker; restart with resume()"
            )
        heartbeat = asyncio.create_task(self._heartbeat(task.id))
        try:
            async for event in live.worker.events(live.handle):
                # ADR-0011: only durable facts are stored; usage is the one M1 records.
                if event.durable and event.kind == "usage":
                    if task.run_id not in self._run_usage:
                        self._restore_run_usage(task.run_id)
                    self._append_task(
                        task.id, [self._event(f"task:{task.id}", "usage.recorded", event.data)]
                    )
                    for name in self._run_usage[task.run_id]:
                        self._run_usage[task.run_id][name] += int(event.data.get(name, 0))
                    self._check_run_budget(task.run_id)
            result = await live.worker.result(live.handle)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        del self._live[task.id]
        task = self.store.get_task(task.id)
        released = self._released(task.id)

        if result.success:
            ws = self._workspaces[task.id]
            diff = await self.workspace.capture_diff(ws)
            self._register_patch(task.id, diff)
            self._transition(
                task,
                S.AWAITING_GATE,
                "worker succeeded",
                before=[self._result(task, result, diff)],
                after=[released],
            )
            return

        before = [self._result(task, result, None)]
        if result.error == "cancelled":
            self._transition(task, S.CANCELLED, "worker cancelled", before=before, after=[released])
        elif (
            _is_transient(result.error)
            and task.spec.idempotent
            and task.attempts < self.max_attempts
        ):
            await self._discard_workspace(task.id)
            self._transition(
                task,
                S.RETRY_WAIT,
                f"transient error: {result.error}",
                before=before,
                after=[released],
            )
        else:
            # The worktree is kept for inspection.
            self._transition(
                task, S.FAILED, f"worker failed: {result.error}", before=before, after=[released]
            )

    async def _heartbeat(self, task_id: str) -> None:
        """Keep this process's lease alive while the worker runs (every ttl/3)."""
        while True:
            await asyncio.sleep(self.lease_ttl_s / 3)
            self.store.renew_lease(task_id, self.holder, self.lease_ttl_s)

    async def _integrate(self, task: TaskRow) -> None:
        """Run the gate under the merge slot (ADR-0005: one integration at a time).

        AWAITING_GATE -> INTEGRATING only happens once the slot is held, so INTEGRATING means
        "at the head of the merge queue". A task found INTEGRATING after recovery queues for
        the slot like any other before its gate is run again.
        """
        async with self._merge_slot:
            await self._integrate_holding_slot(self.store.get_task(task.id))

    async def _integrate_holding_slot(self, task: TaskRow) -> None:
        diff = self._last_diff(task.id)
        run = self._run_info(task.run_id)
        if task.state is S.AWAITING_GATE:
            started = self._event(f"task:{task.id}", "gate.started", {"attempt": task.attempts})
            task = self._transition(task, S.INTEGRATING, "gate started", after=[started])
        else:
            # Re-running the gate after a crash: INTEGRATING -> INTEGRATING is not a transition,
            # so only a second gate.started is recorded, unique to this boot.
            again = self._event(
                f"task:{task.id}",
                "gate.started",
                {"attempt": task.attempts, "rerun_after_recovery": True},
                idempotency_key=f"{task.id}:r{task.revision}:gate:{self.boot_id}",
            )
            self._append_task(task.id, [again])

        async def record_merging(old: str, new: str) -> None:
            self._append_task(
                task.id,
                [self._event(f"task:{task.id}", "gate.merging", {"old": old, "new": new})],
            )
            _chaos_hold("MERGING", task.id)  # recorded, target not moved yet

        patterns = normalize_write_set(task.spec.write_set)
        outside = [
            path for path in diff.files_changed if not any(matches(path, p) for p in patterns)
        ]
        if outside:
            report = GateReport(
                task_id=task.id,
                passed=False,
                checks={"write_set": False},
                output_tail="Files outside write set "
                + repr(patterns)
                + ":\n"
                + "\n".join(outside),
            )
        else:
            report = await self.gate.check(diff, str(run["target_branch"]), record_merging)
        if report.merged_commit:
            _chaos_hold("MERGED", task.id)  # target moved, DONE not recorded yet
        task = self.store.get_task(task.id)
        payload = {"report": report.model_dump(mode="json")}
        passed = self._event(f"task:{task.id}", "gate.passed", payload)
        failed = self._event(f"task:{task.id}", "gate.failed", payload)

        if report.passed and (report.merged_commit or not diff.files_changed):
            self._transition(task, S.DONE, "integrated", before=[passed])
            await self._discard_workspace(task.id)
        elif report.passed:
            self._transition(task, S.NEEDS_HUMAN, "merge not confirmed", before=[passed])
        elif report.needs_human:
            self._transition(task, S.NEEDS_HUMAN, "gate needs a person", before=[failed])
        elif task.attempts < self.max_attempts:
            await self._discard_workspace(task.id)
            self._transition(task, S.REWORK, "gate failed", before=[failed])
        else:
            self._transition(
                task, S.NEEDS_HUMAN, "gate failed, attempts exhausted", before=[failed]
            )

    # --- helpers -------------------------------------------------------------------------------

    def _event(
        self,
        stream: str,
        type_: str,
        payload: dict[str, Any],
        causation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> NewEvent:
        return NewEvent(
            stream=stream,
            type=type_,
            actor=self.actor,
            payload=payload,
            causation_id=causation_id,
            idempotency_key=idempotency_key,
        )

    def _released(self, task_id: str) -> NewEvent:
        return self._event(
            f"task:{task_id}", "task.lease_released", {"task_id": task_id, "holder": self.holder}
        )

    def _expiry(self) -> datetime:
        return self.clock.now() + timedelta(seconds=self.lease_ttl_s)

    def _transition(
        self,
        task: TaskRow,
        to: TaskState,
        reason: str,
        before: Sequence[NewEvent] = (),
        after: Sequence[NewEvent] = (),
        outbox: Sequence[OutboxItem] = (),
    ) -> TaskRow:
        """The only way task state changes: one `task.state_changed` event under stream CAS."""
        change = TaskStateChange(
            task_id=task.id,
            from_state=task.state,
            to_state=to,
            revision=task.revision + 1,
            reason=reason,
        )
        event = self._event(f"task:{task.id}", "task.state_changed", change.model_dump(mode="json"))
        self._append_task(task.id, [*before, event, *after], outbox)
        if task.state in _HOLDS_WORKER_SLOT and to not in _HOLDS_WORKER_SLOT:
            self._worker_slot_freed.set()
        _chaos_hold(to.value, task.id)
        return self.store.get_task(task.id)

    def _append_task(
        self, task_id: str, events: Sequence[NewEvent], outbox: Sequence[OutboxItem] = ()
    ) -> list[Event]:
        # Deliberately synchronous: reading the cached stream_seq, appending and updating the
        # cache happen without an `await` in between, so no other driver on this event loop can
        # interleave. Each driver also only writes its own task's stream. Adding an `await`
        # here would turn the CAS into a race between drivers.
        stream = f"task:{task_id}"
        if stream not in self._stream_seq:
            existing = self.store.read_stream(stream)
            self._stream_seq[stream] = existing[-1].stream_seq if existing else 0
        appended = self.store.append(
            events, expect={stream: self._stream_seq[stream]}, outbox=outbox
        )
        own = [e.stream_seq for e in appended if e.stream == stream]
        if own:
            self._stream_seq[stream] = max(own)
        return appended

    def _run_info(self, run_id: str) -> dict[str, Any]:
        for event in self.store.read_stream(f"run:{run_id}"):
            if event.type == "run.created":
                return event.payload
        raise OrchestratorError(f"run {run_id} has no run.created event")

    def _result(self, task: TaskRow, result: WorkerResult, diff: DiffArtifact | None) -> NewEvent:
        return self._event(
            f"task:{task.id}",
            "worker.result",
            {
                "attempt": task.attempts,
                "success": result.success,
                "summary": result.summary,
                "error": result.error,
                "usage": result.usage.model_dump() if result.usage else None,
                "diff": diff.model_dump(mode="json") if diff else None,
            },
        )

    def _last_diff(self, task_id: str) -> DiffArtifact:
        for event in reversed(self.store.read_stream(f"task:{task_id}")):
            if event.type == "worker.result" and event.payload.get("diff"):
                return DiffArtifact.model_validate(event.payload["diff"])
        raise OrchestratorError(f"task {task_id} is awaiting the gate without a captured diff")

    def _handoff_notes(self, task_id: str) -> list[str]:
        notes = []
        for event in self.store.read_stream(f"task:{task_id}"):
            if event.type == "gate.failed":
                report = GateReport.model_validate(event.payload["report"])
                checks = ", ".join(
                    f"{k}={'ok' if v else 'FAILED'}" for k, v in report.checks.items()
                )
                notes.append(
                    f"A previous attempt failed the integration gate ({checks}). "
                    f"Output tail:\n{report.output_tail}"
                )
        return notes

    def _register_patch(self, task_id: str, diff: DiffArtifact) -> None:
        path, size = (
            self.artifact_info(diff.patch_ref) if self.artifact_info else (diff.patch_ref, 0)
        )
        self.store.register_artifact(diff.patch_ref, "patch", task_id, path, size)

    def _workspace_for(self, task_id: str) -> Workspace | None:
        """The task's current worktree: from memory, or from the last `worker.started` event."""
        if task_id in self._workspaces:
            return self._workspaces[task_id]
        for event in reversed(self.store.read_stream(f"task:{task_id}")):
            if event.type == "worker.started" and "workspace_id" in event.payload:
                p = event.payload
                return Workspace(
                    task_id=p["workspace_id"],
                    path=p["path"],
                    branch=p["branch"],
                    base_commit=p["base_commit"],
                )
        return None

    async def _discard_workspace(self, task_id: str) -> None:
        ws = self._workspace_for(task_id)
        self._workspaces.pop(task_id, None)
        if ws is not None:
            await self.workspace.cleanup(ws, keep=False)
