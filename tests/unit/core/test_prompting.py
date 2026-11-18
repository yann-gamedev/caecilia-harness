"""SerializedPrompter: one question at a time, without stopping other work (backlog M2-03)."""

from __future__ import annotations

import asyncio

from caecilia_harness.core.models import Action, GateReport
from caecilia_harness.core.ports import UserPrompter
from caecilia_harness.core.prompting import SerializedPrompter


def _action(task: str) -> Action:
    return Action(kind="exec", task_id=task, detail={"argv": ["make", task]})


class GatedPrompter:
    """Answers only when the test releases the matching event; records what it was asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.open_now = 0
        self.max_open = 0
        self.release: dict[str, asyncio.Event] = {}

    def gate(self, key: str) -> asyncio.Event:
        return self.release.setdefault(key, asyncio.Event())

    async def _answer(self, key: str) -> bool:
        self.asked.append(key)
        self.open_now += 1
        self.max_open = max(self.max_open, self.open_now)
        try:
            await self.gate(key).wait()
        finally:
            self.open_now -= 1
        return True

    async def ask_approval(self, action: Action) -> bool:
        return await self._answer(str(action.task_id))

    async def confirm_merge(self, report: GateReport) -> bool:
        return await self._answer(f"merge:{report.task_id}")


def test_is_a_user_prompter() -> None:
    assert isinstance(SerializedPrompter(GatedPrompter()), UserPrompter)


async def test_questions_are_asked_one_at_a_time_in_arrival_order() -> None:
    inner = GatedPrompter()
    prompter = SerializedPrompter(inner)
    first = asyncio.create_task(prompter.ask_approval(_action("api")))
    second = asyncio.create_task(prompter.ask_approval(_action("web")))
    third = asyncio.create_task(prompter.confirm_merge(GateReport(task_id="docs", passed=True)))
    await asyncio.sleep(0.01)
    assert inner.asked == ["api"]  # the others wait their turn

    inner.gate("api").set()
    assert await first is True
    await asyncio.sleep(0.01)
    assert inner.asked == ["api", "web"]

    inner.gate("web").set()
    inner.gate("merge:docs").set()
    assert await second is True and await third is True
    assert inner.asked == ["api", "web", "merge:docs"]
    assert inner.max_open == 1


async def test_other_work_continues_while_a_question_waits() -> None:
    inner = GatedPrompter()
    prompter = SerializedPrompter(inner)
    question = asyncio.create_task(prompter.ask_approval(_action("api")))
    ticks = 0
    for _ in range(5):
        await asyncio.sleep(0)
        ticks += 1
    assert ticks == 5 and not question.done()
    inner.gate("api").set()
    assert await question is True


async def test_a_cancelled_question_frees_the_next_one() -> None:
    inner = GatedPrompter()
    prompter = SerializedPrompter(inner)
    first = asyncio.create_task(prompter.ask_approval(_action("api")))
    second = asyncio.create_task(prompter.ask_approval(_action("web")))
    await asyncio.sleep(0.01)
    first.cancel()
    await asyncio.sleep(0.01)
    assert inner.asked == ["api", "web"]
    inner.gate("web").set()
    assert await second is True
