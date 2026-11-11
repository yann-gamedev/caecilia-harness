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
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from caecilia_harness.core.clock import Clock, SystemClock
from caecilia_harness.core.models import (
    Action,
    Actor,
    ContextPack,
    DiffArtifact,
    Event,
    GateReport,
    Lease,
    NewEvent,
    OutboxItem,
    RunHandle,
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

#: hash -> (path, size in bytes); provided by bootstrap so core never touches the filesystem.
ArtifactInfo = Callable[[str], tuple[str, int]]


class OrchestratorError(Exception):
    """The orchestrator cannot make progress (e.g. a RUNNING task without a live worker)."""


@dataclass
class _Live:
    worker: WorkerAdapter
    handle: RunHandle


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
        spec: TaskSpec,
        target_branch: str = "main",
        options: Mapping[str, Any] | None = None,
    ) -> str:
        """Record a new run with its single task (M1). Returns the run id (`spec.run_id`).

        `options` (check command, worker kind, ...) are stored with the run so a later
        `resume` can rebuild the same orchestrator.
        """
        run_id = spec.run_id
        run_created = self._event(
            f"run:{run_id}",
            "run.created",
            {
                "run_id": run_id,
                "goal": goal,
                "repo_path": repo_path,
                "base_commit": base_commit,
                "target_branch": target_branch,
                "budget": spec.budget.model_dump(),
                "options": dict(options or {}),
            },
        )
        task_created = self._event(
            f"task:{spec.id}", "task.created", {"spec": spec.model_dump(mode="json")}
        )
        self.store.append(
            [run_created, task_created], expect={f"run:{run_id}": 0, f"task:{spec.id}": 0}
        )
        return run_id

    async def run(self, run_id: str) -> str:
        """Drive every task of the run until it stops; record and return the run status."""
        for task in self.store.list_tasks(run_id):
            while self.store.get_task(task.id).state not in STOP_STATES:
                await self.advance(task.id)
        states = {task.state for task in self.store.list_tasks(run_id)}
        if states == {S.DONE}:
            status = "done"
        elif S.NEEDS_HUMAN in states:
            status = "needs_human"
        elif S.FAILED in states:
            status = "failed"
        else:
            status = "cancelled"
        self.store.append(
            [self._event(f"run:{run_id}", "run.finished", {"run_id": run_id, "status": status})]
        )
        return status

    async def resume(self, run_id: str) -> str:
        """Recover after a crash, then continue the run from its current state."""
        await self.recover()
        return await self.run(run_id)

    async def advance(self, task_id: str) -> TaskState:
        """Perform exactly one step for the task and return its new state."""
        task = self.store.get_task(task_id)
        match task.state:
            case S.PENDING:
                # M1 runs have a single task, so there are no blockers to wait for.
                self._transition(task, S.READY, "no blockers")
            case S.READY:
                self._lease_and_spawn(task)
            case S.LEASED:
                await self.process_outbox()
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

    async def process_outbox(self) -> None:
        """Perform pending side effects. Safe to call again after a crash."""
        for item in self.store.pending_outbox():
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

    def _lease_and_spawn(self, task: TaskRow) -> None:
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
            ws = await self.workspace.create(workspace_id, str(run["base_commit"]))
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
                    self._append_task(
                        task.id, [self._event(f"task:{task.id}", "usage.recorded", event.data)]
                    )
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

        report = await self.gate.check(diff, str(run["target_branch"]), record_merging)
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
        return self.store.get_task(task.id)

    def _append_task(
        self, task_id: str, events: Sequence[NewEvent], outbox: Sequence[OutboxItem] = ()
    ) -> list[Event]:
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
