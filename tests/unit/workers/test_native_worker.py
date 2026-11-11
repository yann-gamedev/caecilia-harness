"""Native worker loop driven by FakeProvider (backlog M1-08)."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from caecilia_harness.core.models import (
    Budget,
    ContextPack,
    ModelCaps,
    ModelEvent,
    RunHandle,
    TaskSpec,
    WorkerEvent,
    Workspace,
)
from caecilia_harness.core.ports import WorkerAdapter
from caecilia_harness.providers.fake import FakeProvider
from caecilia_harness.security.approval import ApprovalPolicy, ExecRequest
from caecilia_harness.workers.native.prompts import UNTRUSTED_CLOSE, UNTRUSTED_OPEN
from caecilia_harness.workers.native.worker import NativeWorker
from caecilia_harness.workspace.exec_runner import ExecRunner

PY = sys.executable
SPEC = TaskSpec(
    id="t1",
    run_id="r1",
    title="write hello",
    instructions="Create hello.txt containing hi.",
    acceptance=["hello.txt exists"],
)

Approve = Callable[[ExecRequest], Awaitable[bool]]


async def _yes(req: ExecRequest) -> bool:
    return True


def _done(reason: str = "end_turn") -> ModelEvent:
    return ModelEvent(kind="done", data={"stop_reason": reason})


def _error(retryable: bool) -> ModelEvent:
    return ModelEvent(kind="error", data={"message": "overloaded", "retryable": retryable})


def _write(call_id: str, path: str, content: str) -> list[ModelEvent]:
    return [
        FakeProvider.tool_call(call_id, "write_file", {"path": path, "content": content}),
        _done("tool_use"),
    ]


def _summary(text: str = "Wrote hello.txt.") -> list[ModelEvent]:
    return [FakeProvider.text(text), FakeProvider.usage(20, 5), _done()]


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    wt.mkdir()
    return wt


def _worker(provider: FakeProvider, approve: Approve = _yes) -> NativeWorker:
    def factory(ws: Workspace) -> ExecRunner:
        return ExecRunner(Path(ws.path), ApprovalPolicy(), approve, None, task_id=ws.task_id)

    return NativeWorker(provider, "fake-1", factory, retry_backoff_s=0)


async def _start(worker: NativeWorker, worktree: Path, budget: Budget | None = None) -> RunHandle:
    ws = Workspace(task_id="t1", path=str(worktree), branch="caecilia/t1", base_commit="abc")
    return await worker.start(SPEC, ContextPack(task=SPEC), ws, budget or Budget())


async def _drain(worker: NativeWorker, handle: RunHandle) -> list[WorkerEvent]:
    return [event async for event in worker.events(handle)]


def test_is_a_worker_adapter() -> None:
    worker = _worker(FakeProvider([]))
    assert isinstance(worker, WorkerAdapter)
    caps = worker.capabilities()
    assert caps.streams_events and caps.supports_cancel and caps.edits_files
    assert not caps.supports_resume and not caps.needs_network
    assert caps.reports_usage is False  # FakeProvider default caps


def test_reports_usage_follows_model_caps() -> None:
    caps = ModelCaps(
        supports_tools=True,
        supports_streaming=True,
        reports_usage=True,
        context_window=1000,
        max_output_tokens=100,
    )
    assert _worker(FakeProvider([], caps=caps)).capabilities().reports_usage


async def test_writes_file_and_reports_summary(worktree: Path) -> None:
    provider = FakeProvider([_write("c1", "hello.txt", "hi\n"), _summary()])
    worker = _worker(provider)
    handle = await _start(worker, worktree)
    result = await worker.result(handle)

    assert result.success, result.error
    assert result.summary == "Wrote hello.txt."
    assert (worktree / "hello.txt").read_text(encoding="utf-8") == "hi\n"
    assert result.usage is not None and result.usage.input_tokens == 20

    first = provider.requests[0]
    assert first.messages[0].role == "system"
    assert "hello.txt exists" in first.messages[0].content
    assert {tool.name for tool in first.tools} >= {"read_file", "write_file", "run"}

    second = provider.requests[1]
    assistant, tool_msg = second.messages[-2], second.messages[-1]
    assert assistant.role == "assistant" and assistant.tool_calls[0].id == "c1"
    assert tool_msg.role == "tool" and tool_msg.tool_call_id == "c1"
    assert tool_msg.content.startswith(UNTRUSTED_OPEN)
    assert tool_msg.content.rstrip().endswith(UNTRUSTED_CLOSE)


async def test_failed_tool_is_reported_to_the_model(worktree: Path) -> None:
    (worktree / "a.txt").write_text("alpha", encoding="utf-8")
    edit = FakeProvider.tool_call("c1", "edit_file", {"path": "a.txt", "old": "zzz", "new": "y"})
    provider = FakeProvider([[edit, _done("tool_use")], _summary("Could not edit.")])
    worker = _worker(provider)
    result = await worker.result(await _start(worker, worktree))

    assert result.success
    tool_msg = provider.requests[1].messages[-1]
    assert 'ok="false"' in tool_msg.content
    assert "found 0" in tool_msg.content


async def test_step_budget(worktree: Path) -> None:
    provider = FakeProvider([_write(f"c{i}", f"f{i}.txt", "x") for i in range(5)])
    worker = _worker(provider)
    result = await worker.result(await _start(worker, worktree, Budget(max_steps=2)))
    assert not result.success
    assert result.error == "budget_exceeded:steps"
    assert len(provider.requests) == 2


async def test_output_token_budget(worktree: Path) -> None:
    turn = [FakeProvider.text("long"), FakeProvider.usage(10, 500), _done()]
    worker = _worker(FakeProvider([turn]))
    result = await worker.result(await _start(worker, worktree, Budget(max_output_tokens=100)))
    assert result.error == "budget_exceeded:output_tokens"
    assert result.usage is not None and result.usage.output_tokens == 500


async def test_retryable_error_then_success(worktree: Path) -> None:
    provider = FakeProvider([[_error(True)], _summary("fine")])
    worker = _worker(provider)
    result = await worker.result(await _start(worker, worktree))
    assert result.success
    assert result.summary == "fine"
    assert len(provider.requests) == 2


async def test_three_retryable_errors_fail(worktree: Path) -> None:
    provider = FakeProvider([[_error(True)]] * 3 + [_summary()])
    worker = _worker(provider)
    result = await worker.result(await _start(worker, worktree))
    assert not result.success
    assert result.error is not None and "overloaded" in result.error
    assert len(provider.requests) == 3


async def test_non_retryable_error_fails_at_once(worktree: Path) -> None:
    provider = FakeProvider([[_error(False)], _summary()])
    worker = _worker(provider)
    result = await worker.result(await _start(worker, worktree))
    assert not result.success
    assert len(provider.requests) == 1


async def test_soft_cancel_finishes_the_running_step(worktree: Path) -> None:
    run = FakeProvider.tool_call(
        "c1", "run", {"argv": [PY, "-c", "open('ran.txt', 'w').close()"]}
    )
    provider = FakeProvider([[run, _done("tool_use")], _summary(), _summary()])
    handles: list[RunHandle] = []

    async def approve_then_cancel(req: ExecRequest) -> bool:
        await worker.cancel(handles[0], "soft")
        return True

    worker = _worker(provider, approve_then_cancel)
    handles.append(await _start(worker, worktree))
    result = await worker.result(handles[0])

    assert (result.success, result.error) == (False, "cancelled")
    assert (worktree / "ran.txt").exists()  # the running step completed
    assert len(provider.requests) == 1  # no further step was started


async def test_hard_cancel_stops_a_long_command(worktree: Path) -> None:
    sleep = FakeProvider.tool_call("c1", "run", {"argv": [PY, "-c", "import time; time.sleep(30)"]})
    provider = FakeProvider([[sleep, _done("tool_use")], _summary()])
    handles: list[RunHandle] = []

    async def approve_and_schedule_cancel(req: ExecRequest) -> bool:
        loop = asyncio.get_running_loop()
        loop.call_later(0.5, lambda: asyncio.ensure_future(worker.cancel(handles[0], "hard")))
        return True

    worker = _worker(provider, approve_and_schedule_cancel)
    start = time.monotonic()
    handles.append(await _start(worker, worktree))
    result = await worker.result(handles[0])

    assert (result.success, result.error) == (False, "cancelled")
    assert time.monotonic() - start < 10


async def test_events_include_tool_call_and_durable_usage(worktree: Path) -> None:
    worker = _worker(FakeProvider([_write("c1", "hello.txt", "hi"), _summary()]))
    handle = await _start(worker, worktree)
    await worker.result(handle)
    events = await _drain(worker, handle)

    kinds = [event.kind for event in events]
    assert "tool_call" in kinds and "tool_result" in kinds
    usage = [event for event in events if event.kind == "usage"]
    assert usage and all(event.durable for event in usage)
    assert events[-1].durable and events[-1].data["status"] == "finished"


async def test_unknown_handle(worktree: Path) -> None:
    worker = _worker(FakeProvider([]))
    with pytest.raises(KeyError):
        await worker.result(RunHandle(task_id="nope", worker="native", attempt=1))
