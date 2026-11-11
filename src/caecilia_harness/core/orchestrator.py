"""The run loop: drives a task from PENDING to a terminal state (or NEEDS_HUMAN).

Every state change is an event appended with a per-stream CAS (`expect`). Side effects that must
not be repeated (spawning a worker) go through the transactional outbox and re-check the task
state before acting, so processing an outbox item twice is harmless.

Depends only on `core` (ports, models, states); concrete adapters are wired in `bootstrap.py`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from caecilia_harness.core.models import (
    Action,
    Actor,
    ContextPack,
    DiffArtifact,
    Event,
    GateReport,
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
ACTOR = Actor(kind="orchestrator", id="orchestrator")
SPAWN_WORKER = "spawn_worker"
#: States where `run()` stops driving a task: terminal, or waiting for a person.
STOP_STATES = TERMINAL | {S.NEEDS_HUMAN}

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
    ) -> None:
        self.store = store
        self.workspace = workspace
        self.gate = gate
        self.workers = dict(workers)
        self.prompter = prompter
        self.max_attempts = max_attempts
        self.artifact_info = artifact_info
        self._closers = list(closers)
        self._stream_seq: dict[str, int] = {}
        self._live: dict[str, _Live] = {}
        self._workspaces: dict[str, Workspace] = {}

    def close(self) -> None:
        """Release resources handed over by bootstrap (e.g. the DB connection)."""
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
    ) -> str:
        """Record a new run with its single task (M1). Returns the run id (`spec.run_id`)."""
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

    async def advance(self, task_id: str) -> TaskState:
        """Perform exactly one step for the task and return its new state."""
        task = self.store.get_task(task_id)
        match task.state:
            case S.PENDING:
                # M1 runs have a single task, so there are no blockers to wait for.
                self._transition(task, S.READY, "no blockers")
            case S.READY:
                attempt = task.attempts + 1
                spawn = OutboxItem(
                    idempotency_key=f"{task.id}:{attempt}:{SPAWN_WORKER}",
                    kind=SPAWN_WORKER,
                    payload={"task_id": task.id, "attempt": attempt},
                )
                self._transition(task, S.LEASED, f"attempt {attempt}", outbox=[spawn])
            case S.LEASED:
                await self.process_outbox()
                if self.store.get_task(task_id).state is S.LEASED:
                    raise OrchestratorError(f"task {task_id} is LEASED but nothing spawned it")
            case S.RUNNING:
                await self._collect_worker(task)
            case S.RETRY_WAIT:
                self._transition(task, S.READY, "retrying")  # M1: no backoff delay
            case S.AWAITING_GATE:
                await self._integrate(task)
            case S.REWORK:
                self._transition(task, S.READY, "rework with gate report")
            case state if state in STOP_STATES:
                pass
            case state:
                raise OrchestratorError(f"task {task_id}: no M1 handling for state {state}")
        return self.store.get_task(task_id).state

    async def process_outbox(self) -> None:
        """Perform pending side effects. Safe to call again after a crash."""
        for item in self.store.pending_outbox():
            if item.kind != SPAWN_WORKER:
                self.store.mark_outbox(item.id, "failed")
                continue
            try:
                await self._spawn(str(item.payload["task_id"]), int(item.payload["attempt"]))
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

    # --- steps ---------------------------------------------------------------------------------

    async def _spawn(self, task_id: str, attempt: int) -> None:
        task = self.store.get_task(task_id)
        # Re-check before acting: the item may be replayed after it already took effect.
        if task.state is not S.LEASED or task.attempts + 1 != attempt:
            return
        worker = self.workers.get(task.spec.worker)
        if worker is None:
            self._give_up_spawn(task, f"no worker named {task.spec.worker!r}")
            return
        run = self._run_info(task.run_id)
        try:
            ws = await self.workspace.create(task.id, str(run["base_commit"]))
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
            {"worker": handle.worker, "attempt": attempt, "path": ws.path, "branch": ws.branch},
        )
        self._transition(task, S.RUNNING, f"worker {handle.worker} started", before=[started])

    def _give_up_spawn(self, task: TaskRow, reason: str) -> None:
        # LEASED cannot go to NEEDS_HUMAN directly; RECOVERING is the sanctioned path.
        task = self._transition(task, S.RECOVERING, reason)
        self._transition(task, S.NEEDS_HUMAN, reason)

    async def _collect_worker(self, task: TaskRow) -> None:
        live = self._live.get(task.id)
        if live is None:
            raise OrchestratorError(
                f"task {task.id} is RUNNING without a live worker (crash recovery is M1-11)"
            )
        async for event in live.worker.events(live.handle):
            # ADR-0011: only durable facts are stored; usage is the one M1 records.
            if event.durable and event.kind == "usage":
                self._append_task(
                    task.id, [self._event(f"task:{task.id}", "usage.recorded", event.data)]
                )
        result = await live.worker.result(live.handle)
        del self._live[task.id]
        task = self.store.get_task(task.id)

        if result.success:
            ws = self._workspaces[task.id]
            diff = await self.workspace.capture_diff(ws)
            self._register_patch(diff)
            self._transition(
                task, S.AWAITING_GATE, "worker succeeded", before=[self._result(task, result, diff)]
            )
            return

        before = [self._result(task, result, None)]
        if result.error == "cancelled":
            self._transition(task, S.CANCELLED, "worker cancelled", before=before)
        elif (
            _is_transient(result.error)
            and task.spec.idempotent
            and task.attempts < self.max_attempts
        ):
            await self._discard_workspace(task.id)
            self._transition(task, S.RETRY_WAIT, f"transient error: {result.error}", before=before)
        else:
            # The worktree is kept for inspection.
            self._transition(task, S.FAILED, f"worker failed: {result.error}", before=before)

    async def _integrate(self, task: TaskRow) -> None:
        diff = self._last_diff(task.id)
        run = self._run_info(task.run_id)
        started = self._event(f"task:{task.id}", "gate.started", {"attempt": task.attempts})
        task = self._transition(task, S.INTEGRATING, "gate started", after=[started])

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
    ) -> NewEvent:
        return NewEvent(
            stream=stream, type=type_, actor=ACTOR, payload=payload, causation_id=causation_id
        )

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

    def _register_patch(self, diff: DiffArtifact) -> None:
        path, size = (
            self.artifact_info(diff.patch_ref) if self.artifact_info else (diff.patch_ref, 0)
        )
        self.store.register_artifact(diff.patch_ref, "patch", diff.task_id, path, size)

    async def _discard_workspace(self, task_id: str) -> None:
        ws = self._workspaces.pop(task_id, None)
        if ws is not None:
            await self.workspace.cleanup(ws, keep=False)
