"""LLM planner: one tool, graph validation, correction rounds (backlog M2-05)."""

from __future__ import annotations

from typing import Any

import pytest

from caecilia_harness.core.models import ModelEvent, Usage
from caecilia_harness.planning.plan import PlanTask
from caecilia_harness.planning.planner import PLAN_TOOL, Planner, PlannerError
from caecilia_harness.providers.fake import FakeProvider

GOAL = "tambah endpoint health dan halaman status"
SUMMARY = (
    '<untrusted-data source="repo-summary">\n'
    "Tracked files (2):\napi/app.py\nweb/index.html\n</untrusted-data>"
)


def _task(task_id: str, *blocked_by: str, **extra: Any) -> dict[str, Any]:
    task = {
        "id": task_id,
        "title": f"Task {task_id}",
        "instructions": f"Do {task_id}.",
        "acceptance": [f"{task_id} works"],
        "write_set": [f"{task_id}/**"],
        "blocked_by": list(blocked_by),
        "idempotent": True,
    }
    return task | extra


def _submit(call_id: str, *tasks: dict[str, Any], goal: str = GOAL) -> list[ModelEvent]:
    return [
        FakeProvider.text("Here is the plan."),
        FakeProvider.tool_call(call_id, "submit_plan", {"goal": goal, "tasks": list(tasks)}),
        FakeProvider.usage(100, 20),
        ModelEvent(kind="done", data={"stop_reason": "tool_use"}),
    ]


def _planner(*turns: list[ModelEvent], max_tasks: int = 8) -> tuple[Planner, FakeProvider]:
    provider = FakeProvider(list(turns))
    return Planner(provider, "fake-model", max_tasks=max_tasks, retry_backoff_s=0), provider


async def test_valid_plan_in_one_round() -> None:
    planner, provider = _planner(_submit("c1", _task("api"), _task("web", "api")))

    result = await planner.propose(GOAL, SUMMARY)

    assert result.rounds == 1
    assert result.usage == Usage(input_tokens=100, output_tokens=20)
    plan = result.plan
    assert plan.goal == GOAL and plan.version == 1
    assert [t.id for t in plan.tasks] == ["api", "web"]
    assert plan.tasks[1] == PlanTask(
        id="web",
        title="Task web",
        instructions="Do web.",
        acceptance=["web works"],
        write_set=["web/**"],
        blocked_by=["api"],
        idempotent=True,
    )

    [request] = provider.requests
    assert request.model == "fake-model"
    assert request.tools == [PLAN_TOOL]
    system, user = request.messages
    assert system.role == "system" and "8" in system.content and "submit_plan" in system.content
    assert user.role == "user" and GOAL in user.content and SUMMARY in user.content


async def test_the_users_goal_is_kept_even_if_the_model_rewrites_it() -> None:
    planner, _ = _planner(_submit("c1", _task("api"), goal="something else entirely"))
    assert (await planner.propose(GOAL, SUMMARY)).plan.goal == GOAL


async def test_cycle_is_sent_back_and_fixed_in_round_two() -> None:
    planner, provider = _planner(
        _submit("c1", _task("a", "b"), _task("b", "a")),
        _submit("c2", _task("a"), _task("b", "a")),
    )

    result = await planner.propose(GOAL, SUMMARY)

    assert result.rounds == 2
    assert [t.blocked_by for t in result.plan.tasks] == [[], ["a"]]
    assert result.usage == Usage(input_tokens=200, output_tokens=40)
    second = provider.requests[1]
    assistant, feedback = second.messages[-2], second.messages[-1]
    assert assistant.role == "assistant" and assistant.tool_calls[0].id == "c1"
    assert feedback.role == "tool" and feedback.tool_call_id == "c1"
    assert "cycle" in feedback.content
    assert "a -> b -> a" in feedback.content


async def test_reply_without_tool_call_is_asked_again() -> None:
    chatty = [FakeProvider.text("I think you should split it in two."), FakeProvider.usage(10, 5)]
    planner, provider = _planner(chatty, _submit("c1", _task("api")))

    result = await planner.propose(GOAL, SUMMARY)

    assert result.rounds == 2
    nudge = provider.requests[1].messages[-1]
    assert nudge.role == "user" and "submit_plan" in nudge.content


async def test_gives_up_after_max_rounds_with_the_last_problems() -> None:
    chatty = [FakeProvider.text("no tool for you")]
    planner, provider = _planner(chatty, chatty, chatty, chatty)

    with pytest.raises(PlannerError) as info:
        await planner.propose(GOAL, SUMMARY)

    assert len(provider.requests) == 3  # max_rounds
    assert info.value.rounds == 3
    assert any("submit_plan" in problem for problem in info.value.problems)


async def test_too_many_tasks_is_invalid() -> None:
    many = [_task(f"t{i}") for i in range(4)]
    planner, provider = _planner(_submit("c1", *many), _submit("c2", *many[:3]), max_tasks=3)

    result = await planner.propose(GOAL, SUMMARY)

    assert result.rounds == 2 and len(result.plan.tasks) == 3
    feedback = provider.requests[1].messages[-1]
    assert "4 tasks" in feedback.content and "at most 3" in feedback.content


async def test_structural_problems_are_listed_specifically() -> None:
    broken = _task("api")
    del broken["title"]
    planner, provider = _planner(
        _submit("c1", broken, _task("web", write_set=["../x"], unknown=1)),
        _submit("c2", _task("api"), _task("web")),
    )

    assert (await planner.propose(GOAL, SUMMARY)).rounds == 2
    feedback = provider.requests[1].messages[-1].content
    assert "tasks.0.title" in feedback
    assert "tasks.1.unknown" in feedback


async def test_bad_patterns_and_missing_blockers_are_reported() -> None:
    planner, provider = _planner(
        _submit("c1", _task("api", write_set=["/abs/**"]), _task("web", "ghost")),
        _submit("c2", _task("api"), _task("web", "api")),
    )
    assert (await planner.propose(GOAL, SUMMARY)).rounds == 2
    feedback = provider.requests[1].messages[-1].content
    assert "bad_pattern" in feedback and "/abs/**" in feedback
    assert "missing_blocker" in feedback and "ghost" in feedback


async def test_malformed_arguments_are_reported() -> None:
    garbled = [
        FakeProvider.tool_call("c1", "submit_plan", {"_invalid_arguments": "{not json"}),
        ModelEvent(kind="done", data={"stop_reason": "tool_use"}),
    ]
    planner, provider = _planner(garbled, _submit("c2", _task("api")))
    assert (await planner.propose(GOAL, SUMMARY)).rounds == 2
    assert "not valid JSON" in provider.requests[1].messages[-1].content


async def test_retryable_error_is_retried_once_per_round() -> None:
    flaky = [ModelEvent(kind="error", data={"message": "overloaded", "retryable": True})]
    planner, provider = _planner(flaky, _submit("c1", _task("api")))
    result = await planner.propose(GOAL, SUMMARY)
    assert result.rounds == 1
    assert len(provider.requests) == 2


async def test_second_retryable_error_in_a_round_fails() -> None:
    flaky = [ModelEvent(kind="error", data={"message": "overloaded", "retryable": True})]
    planner, _ = _planner(flaky, flaky, _submit("c1", _task("api")))
    with pytest.raises(PlannerError, match="overloaded"):
        await planner.propose(GOAL, SUMMARY)


async def test_non_retryable_error_fails_at_once() -> None:
    fatal = [ModelEvent(kind="error", data={"message": "HTTP 401: bad key", "retryable": False})]
    planner, provider = _planner(fatal, _submit("c1", _task("api")))
    with pytest.raises(PlannerError, match="HTTP 401"):
        await planner.propose(GOAL, SUMMARY)
    assert len(provider.requests) == 1


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def test_tool_schema_is_self_contained() -> None:
    schema = PLAN_TOOL.input_schema
    assert not {"$ref", "$defs", "definitions"} & _keys(schema)
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"goal", "tasks"}
    task = schema["properties"]["tasks"]["items"]
    assert set(task["properties"]) == {
        "id",
        "title",
        "instructions",
        "acceptance",
        "write_set",
        "blocked_by",
        "idempotent",
    }
