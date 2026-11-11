"""Ports: the interfaces core depends on. Adapters in other packages implement them.

Changing a signature here is an architecture change: update docs/architecture.md and the
contract test suites in the same change.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Literal, Protocol, runtime_checkable

from caecilia_harness.core.models import (
    Action,
    Budget,
    ContextPack,
    Decision,
    DiffArtifact,
    Event,
    GateReport,
    ModelCaps,
    ModelEvent,
    ModelRequest,
    NewEvent,
    Principal,
    RunHandle,
    TaskSpec,
    WorkerCapabilities,
    WorkerEvent,
    WorkerResult,
    Workspace,
)


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
    async def check(self, diff: DiffArtifact, target_branch: str) -> GateReport: ...


@runtime_checkable
class StateStore(Protocol):
    def append(
        self, events: Sequence[NewEvent], expect: Mapping[str, int] | None = None
    ) -> list[Event]:
        """Append atomically. `expect` maps stream -> last known stream_seq (CAS)."""
        ...

    def read_stream(self, stream: str, after: int = 0) -> list[Event]: ...

    def cas_document(self, doc_id: str, expected_revision: int, content_ref: str) -> int:
        """Return the new revision, or raise if `expected_revision` is stale."""
        ...


@runtime_checkable
class PolicyEngine(Protocol):
    def decide(self, action: Action, principal: Principal) -> Decision: ...
