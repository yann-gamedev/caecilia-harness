"""Tight slots never deadlock: provider, exec and max_parallel together (backlog M2-07)."""

from __future__ import annotations

import asyncio
import random
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from caecilia_harness.bootstrap import artifact_info, exec_approver
from caecilia_harness.core.models import (
    Action,
    GateReport,
    ModelCaps,
    ModelEvent,
    ModelRequest,
    TaskSpec,
    Workspace,
)
from caecilia_harness.core.orchestrator import Orchestrator
from caecilia_harness.core.ports import CancelToken, UserPrompter
from caecilia_harness.core.prompting import SerializedPrompter
from caecilia_harness.core.states import TaskState
from caecilia_harness.gate.simple import SimpleGate
from caecilia_harness.paths import db_path
from caecilia_harness.providers.fake import FakeProvider
from caecilia_harness.providers.limited import LimitedProvider
from caecilia_harness.security.approval import ApprovalPolicy, ExecRequest
from caecilia_harness.store.db import open_db
from caecilia_harness.store.events import SqliteEventStore
from caecilia_harness.store.projections import project_runs_and_tasks
from caecilia_harness.workers.native.worker import NativeWorker
from caecilia_harness.workspace.exec_runner import ExecRunner
from caecilia_harness.workspace.git_worktree import GitWorktreeBackend
from tests.integration.conftest import git

PY = sys.executable
TASKS = [f"t{i}" for i in range(6)]
RUNS_PER_TASK = 2
TIME_LIMIT_S = 60

CAPS = ModelCaps(
    supports_tools=True,
    supports_streaming=True,
    reports_usage=True,
    context_window=100_000,
    max_output_tokens=4_096,
)


class ScriptedModel:
    """A model that, per task: writes one file, runs a command a few times, then finishes.

    The step is the number of tool results in the request, so every task follows its own script
    however the requests interleave. Delays come from (seed, task, step): random-looking but
    the same on every run with that seed.
    """

    id = "scripted"

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.active = 0
        self.peak = 0

    def capabilities(self, model: str) -> ModelCaps:
        return CAPS

    def stream(self, req: ModelRequest, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        return self._reply(req)

    async def _reply(self, req: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            task = next(
                line.removeprefix("Task: ")
                for line in req.messages[1].content.splitlines()
                if line.startswith("Task: ")
            )
            step = sum(message.role == "tool" for message in req.messages)
            delay = random.Random(f"{self.seed}:{task}:{step}").uniform(0, 0.05)  # noqa: S311
            await asyncio.sleep(delay)
            yield FakeProvider.usage(10, 2)
            if step == 0:
                yield FakeProvider.tool_call(
                    f"{task}-w", "write_file", {"path": f"{task}/out.txt", "content": task}
                )
            elif step <= RUNS_PER_TASK:
                yield FakeProvider.tool_call(f"{task}-r{step}", "run", {"argv": [PY, "-c", "pass"]})
            else:
                yield FakeProvider.text(f"{task} selesai")
            yield ModelEvent(
                kind="done",
                data={"stop_reason": "end_turn" if step > RUNS_PER_TASK else "tool_use"},
            )
        finally:
            self.active -= 1


class ApproveAll:
    def __init__(self) -> None:
        self.asked: list[Action] = []

    async def ask_approval(self, action: Action) -> bool:
        self.asked.append(action)
        return True

    async def confirm_merge(self, report: GateReport) -> bool:
        return True


@pytest.fixture
def store() -> Iterator[SqliteEventStore]:
    s = SqliteEventStore(open_db(db_path()), projectors=[project_runs_and_tasks])
    try:
        yield s
    finally:
        s.close()


class CountingSlots(asyncio.Semaphore):
    """A semaphore that remembers how many holders it had at once."""

    def __init__(self, value: int) -> None:
        super().__init__(value)
        self.held = 0
        self.peak = 0

    async def acquire(self) -> bool:
        await super().acquire()
        self.held += 1
        self.peak = max(self.peak, self.held)
        return True

    def release(self) -> None:
        self.held -= 1
        super().release()


def _orchestrator(
    store: SqliteEventStore, repo: Path, model: ScriptedModel, prompter: UserPrompter
) -> tuple[Orchestrator, CountingSlots, CountingSlots]:
    provider_slots, exec_slots = CountingSlots(1), CountingSlots(1)
    orchestrator: list[Orchestrator] = []

    async def approve(req: ExecRequest) -> bool:
        return await exec_approver(orchestrator[0])(req)

    def runner_for(ws: Workspace) -> ExecRunner:
        return ExecRunner(
            Path(ws.path), ApprovalPolicy(), approve, None, task_id=ws.task_id, slots=exec_slots
        )

    worker = NativeWorker(
        LimitedProvider(model, provider_slots), "scripted", runner_for, retry_backoff_s=0
    )
    questions = SerializedPrompter(prompter)
    gate = SimpleGate(
        repo, [PY, "-c", "print(1)"], confirm=questions.confirm_merge, auto_merge=True
    )
    orch = Orchestrator(
        store,
        GitWorktreeBackend(repo),
        gate,
        {"native": worker},
        questions,
        artifact_info=artifact_info,
    )
    orchestrator.append(orch)
    return orch, provider_slots, exec_slots


def _create(orch: Orchestrator, repo: Path) -> str:
    specs = [
        TaskSpec(
            id=f"r1-{task}",
            run_id="r1",
            title=task,
            instructions=f"tulis {task}/out.txt",
            write_set=[f"{task}/**"],
            worker="native",
        )
        for task in TASKS
    ]
    base = git(repo, "rev-parse", "main")
    return orch.create_run("stres", str(repo), base, specs, "main", {}, max_parallel=3)


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
async def test_six_native_tasks_with_one_provider_and_one_exec_slot(
    store: SqliteEventStore, git_repo: Path, seed: int
) -> None:
    model = ScriptedModel(seed)
    prompter = ApproveAll()
    orch, provider_slots, exec_slots = _orchestrator(store, git_repo, model, prompter)
    run_id = _create(orch, git_repo)

    status = await asyncio.wait_for(orch.run(run_id), TIME_LIMIT_S)

    assert status == "done"
    assert all(task.state is TaskState.DONE for task in store.list_tasks(run_id))
    assert model.peak == 1 and provider_slots.peak == 1
    assert exec_slots.peak == 1
    assert len(prompter.asked) == len(TASKS) * RUNS_PER_TASK
    for task in TASKS:
        assert git(git_repo, "show", f"main:{task}/out.txt") == task


async def test_a_waiting_question_does_not_stop_other_tasks(
    store: SqliteEventStore, git_repo: Path
) -> None:
    seen_meanwhile: list[str] = []

    class SlowFirstAnswer(ApproveAll):
        async def ask_approval(self, action: Action) -> bool:
            if not self.asked:
                before = store.read_all()[-1].seq
                self.asked.append(action)
                await asyncio.sleep(1.0)  # a person still thinking about the first question
                asker = f"task:{action.task_id}"
                seen_meanwhile.extend(
                    e.stream
                    for e in store.read_all(after_seq=before)
                    if e.stream.startswith("task:") and e.stream != asker
                )
                return True
            return await super().ask_approval(action)

    model = ScriptedModel(seed=7)
    orch, _, _ = _orchestrator(store, git_repo, model, SlowFirstAnswer())
    run_id = _create(orch, git_repo)

    assert await asyncio.wait_for(orch.run(run_id), TIME_LIMIT_S) == "done"
    assert seen_meanwhile, "no other task made progress while the question was open"
