"""Deterministic worker for tests, chaos tests and `caecilia run --worker fake` demos.

It writes fixed files into the worktree; it never calls a model or runs commands.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from caecilia_harness.core.models import (
    Budget,
    ContextPack,
    RunHandle,
    TaskSpec,
    WorkerCapabilities,
    WorkerEvent,
    WorkerResult,
    Workspace,
)
from caecilia_harness.workers.errors import UnknownRunHandleError

#: Written when no `files` are given: the task instructions (the CLI goal) as a note.
DEFAULT_FILE = "CAECILIA_FAKE.md"


@dataclass
class _FakeRun:
    task: asyncio.Task[WorkerResult]
    events: list[WorkerEvent] = field(default_factory=list)


class FakeWorker:
    """`core.ports.WorkerAdapter` that writes `files` and reports success (or `fail_with`)."""

    id: str = "fake"

    def __init__(
        self,
        files: dict[str, str] | None = None,
        delay_s: float = 0.0,
        fail_with: str | None = None,
        spawn_log: Path | None = None,
    ) -> None:
        self.files = files
        self.delay_s = delay_s
        self.fail_with = fail_with
        self.spawn_log = spawn_log
        #: Every context pack received, in start order (tests inspect handoff notes).
        self.contexts: list[ContextPack] = []
        self._runs: dict[tuple[str, int], _FakeRun] = {}
        self._attempts: dict[str, int] = {}

    def capabilities(self) -> WorkerCapabilities:
        return WorkerCapabilities(
            streams_events=True,
            supports_cancel=True,
            supports_resume=False,
            reports_usage=False,
            edits_files=True,
            needs_network=False,
        )

    async def start(
        self, spec: TaskSpec, ctx: ContextPack, ws: Workspace, budget: Budget
    ) -> RunHandle:
        attempt = self._attempts.get(spec.id, 0) + 1
        self._attempts[spec.id] = attempt
        handle = RunHandle(task_id=spec.id, worker=self.id, attempt=attempt)
        self.contexts.append(ctx)
        if self.spawn_log is not None:
            # One line per start: chaos tests count these to detect duplicate spawns.
            with self.spawn_log.open("a", encoding="utf-8") as fh:
                fh.write(f"{spec.id}:{attempt}\n")
        run = _FakeRun(task=asyncio.create_task(self._work(spec, Path(ws.path))))
        self._runs[(handle.task_id, handle.attempt)] = run
        return handle

    async def _work(self, spec: TaskSpec, worktree: Path) -> WorkerResult:
        files = self.files if self.files is not None else {DEFAULT_FILE: spec.instructions + "\n"}
        for rel, content in files.items():
            target = worktree / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail_with is not None:
            return WorkerResult(task_id=spec.id, success=False, summary="", error=self.fail_with)
        return WorkerResult(task_id=spec.id, success=True, summary=f"wrote {', '.join(files)}")

    async def events(self, handle: RunHandle) -> AsyncIterator[WorkerEvent]:
        run = self._get(handle)
        yield WorkerEvent(kind="progress", data={"status": "started"})
        result = await self._finish(run, handle)
        yield WorkerEvent(
            kind="progress",
            data={"status": "finished", "success": result.success, "error": result.error},
            durable=True,
        )

    async def cancel(self, handle: RunHandle, mode: Literal["soft", "hard"]) -> None:
        self._get(handle).task.cancel()

    async def result(self, handle: RunHandle) -> WorkerResult:
        return await self._finish(self._get(handle), handle)

    async def _finish(self, run: _FakeRun, handle: RunHandle) -> WorkerResult:
        try:
            return await asyncio.shield(run.task)
        except asyncio.CancelledError:
            if not run.task.cancelled():
                raise
            return WorkerResult(
                task_id=handle.task_id, success=False, summary="", error="cancelled"
            )

    def _get(self, handle: RunHandle) -> _FakeRun:
        try:
            return self._runs[(handle.task_id, handle.attempt)]
        except KeyError:
            raise UnknownRunHandleError(handle) from None
