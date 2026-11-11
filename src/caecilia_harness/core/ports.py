"""Ports: the interfaces core depends on. Adapters in other packages implement them.

Changing a signature here is an architecture change: update docs/architecture.md and the
contract test suites in the same change.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Literal, Protocol, runtime_checkable

from caecilia_harness.core.models import (
    Action,
    Budget,
    ContextPack,
    Decision,
    DiffArtifact,
    Event,
    GateReport,
    Lease,
    ModelCaps,
    ModelEvent,
    ModelRequest,
    NewEvent,
    OutboxItem,
    OutboxRecord,
    Principal,
    RunHandle,
    TaskRow,
    TaskSpec,
    WorkerCapabilities,
    WorkerEvent,
    WorkerResult,
    Workspace,
)
from caecilia_harness.core.states import TaskState

#: Awaited by a gate right before the target branch moves: (old_commit, new_commit).
BeforeMerge = Callable[[str, str], Awaitable[None]]


class CancelToken(Protocol):
    @property
    def cancelled(self) -> bool: ...


@runtime_checkable
class ModelProvider(Protocol):
    id: str

    def capabilities(self, model: str) -> ModelCaps: ...

    def stream(self, req: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        """Yield events; must end with exactly one `done` or `error` event."""
        ...


@runtime_checkable
class WorkerAdapter(Protocol):
    id: str

    def capabilities(self) -> WorkerCapabilities: ...

    async def start(
        self, spec: TaskSpec, ctx: ContextPack, ws: Workspace, budget: Budget
    ) -> RunHandle: ...

    def events(self, handle: RunHandle) -> AsyncIterator[WorkerEvent]: ...

    async def cancel(self, handle: RunHandle, mode: Literal["soft", "hard"]) -> None: ...

    async def result(self, handle: RunHandle) -> WorkerResult: ...


@runtime_checkable
class WorkspaceBackend(Protocol):
    async def create(self, task_id: str, base_commit: str) -> Workspace: ...

    async def capture_diff(self, ws: Workspace) -> DiffArtifact:
        """Diff against `ws.base_commit`, including untracked (non-ignored) files."""
        ...

    async def cleanup(self, ws: Workspace, keep: bool) -> None: ...


@runtime_checkable
class Gate(Protocol):
    async def check(
        self,
        diff: DiffArtifact,
        target_branch: str,
        before_merge: BeforeMerge | None = None,
    ) -> GateReport:
        """Integrate `diff` into `target_branch`.

        `before_merge(old, new)` is awaited right before the target moves, so the caller can
        record which merge is in flight (crash recovery).
        """
        ...

    async def branch_head(self, branch: str) -> str:
        """Current commit of `branch` (recovery compares it with a recorded merge)."""
        ...


@runtime_checkable
class StateStore(Protocol):
    def append(
        self,
        events: Sequence[NewEvent],
        expect: Mapping[str, int] | None = None,
        outbox: Sequence[OutboxItem] = (),
    ) -> list[Event]:
        """Append atomically. `expect` maps stream -> last known stream_seq (CAS).

        `outbox` items are inserted in the same transaction; an `idempotency_key` that already
        exists is ignored.
        """
        ...

    def get_task(self, task_id: str) -> TaskRow: ...

    def list_tasks(
        self, run_id: str | None, states: Sequence[TaskState] | None = None
    ) -> list[TaskRow]:
        """Tasks of `run_id` (all runs if None), optionally filtered by state."""
        ...

    def pending_outbox(self) -> list[OutboxRecord]: ...

    def mark_outbox(self, outbox_id: int, status: str) -> None: ...

    def register_artifact(
        self, hash: str, kind: str, task_id: str | None, path: str, bytes: int
    ) -> None: ...

    def acquire_lease(self, task_id: str, holder: str, attempt: int, ttl_s: float) -> Lease:
        """Record `task.lease_acquired`; raise if another holder's lease is still active."""
        ...

    def renew_lease(self, task_id: str, holder: str, ttl_s: float) -> bool:
        """Extend `holder`'s lease (heartbeat, no event). False if `holder` no longer has it."""
        ...

    def release_lease(self, task_id: str, holder: str) -> None:
        """Record `task.lease_released`."""
        ...

    def list_leases(self) -> list[Lease]: ...

    def read_stream(self, stream: str, after: int = 0) -> list[Event]: ...

    def cas_document(self, doc_id: str, expected_revision: int, content_ref: str) -> int:
        """Return the new revision, or raise if `expected_revision` is stale."""
        ...


@runtime_checkable
class UserPrompter(Protocol):
    """How the orchestrator asks a person (terminal in M1, TUI/GUI later)."""

    async def ask_approval(self, action: Action) -> bool: ...

    async def confirm_merge(self, report: GateReport) -> bool: ...


@runtime_checkable
class PolicyEngine(Protocol):
    def decide(self, action: Action, principal: Principal) -> Decision: ...
