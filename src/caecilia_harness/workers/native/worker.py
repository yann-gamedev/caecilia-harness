"""Native worker: drives a `ModelProvider` and the worktree tools until the task is done.

First `WorkerAdapter` implementation (ADR-0006). It never touches the store; progress is
reported through `events()` and the final `WorkerResult`.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from caecilia_harness.core.cancel import CancelFlag
from caecilia_harness.core.models import (
    Budget,
    ContextPack,
    Message,
    ModelRequest,
    RunHandle,
    TaskSpec,
    ToolCall,
    Usage,
    WorkerCapabilities,
    WorkerEvent,
    WorkerResult,
    Workspace,
)
from caecilia_harness.core.ports import ModelProvider
from caecilia_harness.workers.errors import UnknownRunHandleError
from caecilia_harness.workers.native.prompts import (
    build_system_prompt,
    build_user_message,
    wrap_untrusted,
)
from caecilia_harness.workers.native.tools import TOOL_SPECS, ToolContext, dispatch
from caecilia_harness.workspace.exec_runner import ExecRunner

DEFAULT_MAX_STEPS = 50
MAX_RETRIES = 2  # extra attempts of one step after a retryable provider error
EVENT_BUFFER = 1000


class _EventBuffer:
    """Bounded event buffer that never blocks the worker.

    When full, ephemeral events are dropped; durable ones (ADR-0011) evict the oldest
    ephemeral event instead, or are kept beyond the bound if there is none.
    """

    def __init__(self, limit: int = EVENT_BUFFER) -> None:
        self._items: deque[WorkerEvent] = deque()
        self._limit = limit
        self._changed = asyncio.Event()
        self._closed = False
        self.dropped = 0

    def put(self, event: WorkerEvent) -> None:
        if len(self._items) >= self._limit:
            if not event.durable:
                self.dropped += 1
                return
            for i, queued in enumerate(self._items):
                if not queued.durable:
                    del self._items[i]
                    self.dropped += 1
                    break
        self._items.append(event)
        self._changed.set()

    def close(self) -> None:
        self._closed = True
        self._changed.set()

    async def __aiter__(self) -> AsyncIterator[WorkerEvent]:
        while True:
            while self._items:
                yield self._items.popleft()
            if self._closed:
                return
            self._changed.clear()
            await self._changed.wait()


@dataclass
class _Run:
    spec: TaskSpec
    events: _EventBuffer = field(default_factory=_EventBuffer)
    soft_cancel: bool = False
    hard_cancel: bool = False
    provider_cancel: CancelFlag = field(default_factory=CancelFlag)
    usage: Usage = field(default_factory=Usage)
    task: asyncio.Task[WorkerResult] | None = None


class _Stop(Exception):
    """Ends the loop with a failed result carrying `error`."""

    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


@dataclass
class _Turn:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    error: dict[str, Any] | None = None
    stop_reason: str | None = None


class NativeWorker:
    """`core.ports.WorkerAdapter` running the model/tool loop in-process."""

    id: str = "native"

    def __init__(
        self,
        provider: ModelProvider,
        model: str,
        exec_runner_factory: Callable[[Workspace], ExecRunner],
        retry_backoff_s: float = 0.5,
    ) -> None:
        self.provider = provider
        self.model = model
        self.exec_runner_factory = exec_runner_factory
        self.retry_backoff_s = retry_backoff_s
        self._runs: dict[tuple[str, int], _Run] = {}
        self._attempts: dict[str, int] = {}

    def capabilities(self) -> WorkerCapabilities:
        return WorkerCapabilities(
            streams_events=True,
            supports_cancel=True,
            supports_resume=False,
            reports_usage=self.provider.capabilities(self.model).reports_usage,
            edits_files=True,
            needs_network=False,
        )

    async def start(
        self, spec: TaskSpec, ctx: ContextPack, ws: Workspace, budget: Budget
    ) -> RunHandle:
        attempt = self._attempts.get(spec.id, 0) + 1
        self._attempts[spec.id] = attempt
        handle = RunHandle(task_id=spec.id, worker=self.id, attempt=attempt)
        run = _Run(spec=spec)
        self._runs[(handle.task_id, handle.attempt)] = run
        run.task = asyncio.create_task(self._main(run, ctx, ws, budget))
        return handle

    def events(self, handle: RunHandle) -> AsyncIterator[WorkerEvent]:
        return aiter(self._run(handle).events)

    async def cancel(self, handle: RunHandle, mode: Literal["soft", "hard"]) -> None:
        run = self._run(handle)
        run.soft_cancel = True
        if mode == "hard" and run.task is not None and not run.task.done():
            run.hard_cancel = True
            run.provider_cancel.cancel()
            run.task.cancel()

    async def result(self, handle: RunHandle) -> WorkerResult:
        run = self._run(handle)
        assert run.task is not None  # noqa: S101 - set in start()
        return await asyncio.shield(run.task)

    def _run(self, handle: RunHandle) -> _Run:
        try:
            return self._runs[(handle.task_id, handle.attempt)]
        except KeyError:
            raise UnknownRunHandleError(handle) from None

    # --- loop ------------------------------------------------------------------------------

    async def _main(
        self, run: _Run, ctx: ContextPack, ws: Workspace, budget: Budget
    ) -> WorkerResult:
        try:
            summary = await self._loop(run, ctx, ws, budget)
            result = self._result(run, success=True, summary=summary)
        except _Stop as stop:
            result = self._result(run, success=False, error=stop.error)
        except asyncio.CancelledError:
            if not run.hard_cancel:
                run.events.close()
                raise  # cancelled by someone else (e.g. loop shutdown): do not swallow
            result = self._result(run, success=False, error="cancelled")
        except Exception as exc:  # report, never crash the orchestrator
            result = self._result(run, success=False, error=f"worker_crashed:{exc!r}")
        run.events.put(
            WorkerEvent(
                kind="progress",
                data={
                    "status": "finished",
                    "success": result.success,
                    "summary": result.summary,
                    "error": result.error,
                },
                durable=True,
            )
        )
        run.events.close()
        return result

    def _result(
        self, run: _Run, success: bool, summary: str = "", error: str | None = None
    ) -> WorkerResult:
        return WorkerResult(
            task_id=run.spec.id, success=success, summary=summary, usage=run.usage, error=error
        )

    async def _loop(self, run: _Run, ctx: ContextPack, ws: Workspace, budget: Budget) -> str:
        tools = ToolContext(worktree=Path(ws.path), exec_runner=self.exec_runner_factory(ws))
        messages = [
            Message(role="system", content=build_system_prompt(run.spec)),
            Message(role="user", content=build_user_message(run.spec, ctx)),
        ]
        max_steps = budget.max_steps if budget.max_steps is not None else DEFAULT_MAX_STEPS
        last_text = ""
        step = 0
        while True:
            # Soft cancel and the step budget are checked only between steps.
            if run.soft_cancel:
                raise _Stop("cancelled")
            if step >= max_steps:
                raise _Stop("budget_exceeded:steps")
            step += 1
            run.events.put(WorkerEvent(kind="progress", data={"step": step}))

            turn = await self._call_model(run, messages)
            self._check_tokens(run, budget)
            if turn.stop_reason == "cancelled":
                raise _Stop("cancelled")
            last_text = turn.text or last_text
            messages.append(Message(role="assistant", content=turn.text, tool_calls=turn.calls))
            if not turn.calls:
                return last_text

            for call in turn.calls:
                run.events.put(
                    WorkerEvent(
                        kind="tool_call",
                        data={"id": call.id, "name": call.name, "input": call.input},
                    )
                )
                outcome = await dispatch(tools, call.name, call.input)
                run.events.put(
                    WorkerEvent(
                        kind="tool_result",
                        data={"id": call.id, "ok": outcome.ok, "chars": len(outcome.content)},
                    )
                )
                content = wrap_untrusted(outcome.content, f"tool:{call.name}", ok=outcome.ok)
                messages.append(Message(role="tool", content=content, tool_call_id=call.id))

    async def _call_model(self, run: _Run, messages: list[Message]) -> _Turn:
        request = ModelRequest(model=self.model, messages=list(messages), tools=TOOL_SPECS)
        for attempt in range(MAX_RETRIES + 1):
            turn = await self._stream_once(run, request)
            if turn.error is None:
                return turn
            if not turn.error.get("retryable") or attempt == MAX_RETRIES:
                raise _Stop(f"provider_error:{turn.error.get('message', 'unknown')}")
            run.events.put(
                WorkerEvent(kind="log", data={"retry": attempt + 1, "error": turn.error})
            )
            await asyncio.sleep(self.retry_backoff_s * (2**attempt))
        raise AssertionError("unreachable")  # pragma: no cover

    async def _stream_once(self, run: _Run, request: ModelRequest) -> _Turn:
        turn = _Turn()
        texts: list[str] = []
        async for event in self.provider.stream(request, run.provider_cancel):
            if event.kind == "text_delta":
                texts.append(str(event.data.get("text", "")))
            elif event.kind == "tool_call":
                turn.calls.append(
                    ToolCall(
                        id=str(event.data["id"]),
                        name=str(event.data["name"]),
                        input=dict(event.data.get("input") or {}),
                    )
                )
            elif event.kind == "usage":
                turn.usage = Usage(
                    input_tokens=turn.usage.input_tokens + int(event.data["input_tokens"]),
                    output_tokens=turn.usage.output_tokens + int(event.data["output_tokens"]),
                )
            elif event.kind == "error":
                turn.error = dict(event.data)
            elif event.kind == "done":
                turn.stop_reason = str(event.data.get("stop_reason"))
        turn.text = "".join(texts)
        # Usage is real spend even if the attempt failed, so it always counts.
        if turn.usage != Usage():
            run.usage = Usage(
                input_tokens=run.usage.input_tokens + turn.usage.input_tokens,
                output_tokens=run.usage.output_tokens + turn.usage.output_tokens,
            )
            run.events.put(WorkerEvent(kind="usage", data=turn.usage.model_dump(), durable=True))
        return turn

    def _check_tokens(self, run: _Run, budget: Budget) -> None:
        if budget.max_input_tokens is not None and run.usage.input_tokens > budget.max_input_tokens:
            raise _Stop("budget_exceeded:input_tokens")
        if (
            budget.max_output_tokens is not None
            and run.usage.output_tokens > budget.max_output_tokens
        ):
            raise _Stop("budget_exceeded:output_tokens")


__all__ = ["NativeWorker"]
