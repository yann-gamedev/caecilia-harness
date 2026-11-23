"""LLM planner: turn a goal into a validated `Plan` (dependency-aware decomposition, ADR-0013).

The planner only proposes. It never starts a run: the plan is written to a file the person
reviews and then runs with `caecilia run --plan`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from caecilia_harness.core.cancel import CancelFlag
from caecilia_harness.core.graph import GraphNode, validate_graph
from caecilia_harness.core.models import Message, ModelRequest, ToolCall, ToolSpec, Usage
from caecilia_harness.core.ports import ModelProvider
from caecilia_harness.planning.plan import Plan
from caecilia_harness.planning.prompts import (
    build_system_prompt,
    build_user_message,
    no_tool_call_message,
    problems_message,
)

TOOL_NAME = "submit_plan"

_STRINGS = {"type": "array", "items": {"type": "string"}}

# Written by hand without $ref/$defs: not every provider resolves references in tool schemas.
PLAN_TOOL = ToolSpec(
    name=TOOL_NAME,
    description="Submit the complete task plan for the goal. Call exactly once per answer.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["goal", "tasks"],
        "properties": {
            "goal": {"type": "string", "description": "The goal being planned."},
            "tasks": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "id",
                        "title",
                        "instructions",
                        "acceptance",
                        "write_set",
                        "blocked_by",
                    ],
                    "properties": {
                        "id": {"type": "string", "pattern": "^[A-Za-z0-9_-]+$"},
                        "title": {"type": "string"},
                        "instructions": {"type": "string"},
                        "acceptance": {**_STRINGS, "description": "Checkable criteria."},
                        "write_set": {**_STRINGS, "description": "Repo-relative glob patterns."},
                        "blocked_by": {**_STRINGS, "description": "Ids of tasks needed first."},
                        "idempotent": {
                            "type": "boolean",
                            "description": "Safe to run again after a crash (default true).",
                        },
                    },
                },
            },
        },
    },
)


class PlannerError(Exception):
    """No valid plan: the provider failed, or every round was rejected."""

    def __init__(self, message: str, problems: Sequence[str] = (), rounds: int = 0) -> None:
        self.problems = list(problems)
        self.rounds = rounds
        detail = "".join(f"\n- {p}" for p in self.problems)
        super().__init__(message + detail)


@dataclass(frozen=True)
class PlanResult:
    plan: Plan
    usage: Usage
    rounds: int


@dataclass
class _Reply:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    error: dict[str, Any] | None = None


def _add(a: Usage, b: Usage) -> Usage:
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
    )


class Planner:
    def __init__(
        self,
        provider: ModelProvider,
        model: str,
        max_tasks: int = 8,
        max_rounds: int = 3,
        retry_backoff_s: float = 0.5,
    ) -> None:
        self.provider = provider
        self.model = model
        self.max_tasks = max_tasks
        self.max_rounds = max_rounds
        self.retry_backoff_s = retry_backoff_s

    async def propose(self, goal: str, repo_summary: str) -> PlanResult:
        """Ask the model for a plan; send problems back until it is valid or rounds run out."""
        messages = [
            Message(role="system", content=build_system_prompt(self.max_tasks)),
            Message(role="user", content=build_user_message(goal, repo_summary)),
        ]
        usage = Usage()
        problems: list[str] = []
        for round_number in range(1, self.max_rounds + 1):
            reply = await self._ask(messages)
            usage = _add(usage, reply.usage)
            messages.append(Message(role="assistant", content=reply.text, tool_calls=reply.calls))
            submit = next((call for call in reply.calls if call.name == TOOL_NAME), None)
            # Every tool call must be answered, including ones for tools that do not exist.
            for call in reply.calls:
                if call is not submit:
                    messages.append(
                        Message(
                            role="tool",
                            tool_call_id=call.id,
                            content=f"unknown tool {call.name!r}; only {TOOL_NAME} exists",
                        )
                    )
            if submit is None:
                problems = [f"the reply did not call {TOOL_NAME}"]
                messages.append(Message(role="user", content=no_tool_call_message()))
                continue
            plan, problems = self._check(goal, submit.input)
            if plan is not None:
                return PlanResult(plan=plan, usage=usage, rounds=round_number)
            messages.append(
                Message(role="tool", tool_call_id=submit.id, content=problems_message(problems))
            )
        raise PlannerError(
            f"no valid plan after {self.max_rounds} rounds", problems, rounds=self.max_rounds
        )

    def _check(self, goal: str, data: dict[str, Any]) -> tuple[Plan | None, list[str]]:
        if "_invalid_arguments" in data:
            return None, ["the submit_plan arguments were not valid JSON"]
        try:
            # The person's goal stays authoritative even if the model rephrases it.
            plan = Plan.model_validate({"version": 1, "goal": goal, "task": data.get("tasks")})
        except ValidationError as exc:
            return None, [_describe(error) for error in exc.errors()]
        problems: list[str] = []
        if len(plan.tasks) > self.max_tasks:
            problems.append(f"the plan has {len(plan.tasks)} tasks; use at most {self.max_tasks}")
        issues = validate_graph(
            [
                GraphNode(id=t.id, blocked_by=tuple(t.blocked_by), write_set=tuple(t.write_set))
                for t in plan.tasks
            ]
        )
        problems += [f"task {i.task_id}: {i.kind}: {i.detail}" for i in issues]
        return (None, problems) if problems else (plan, [])

    async def _ask(self, messages: list[Message]) -> _Reply:
        request = ModelRequest(model=self.model, messages=list(messages), tools=[PLAN_TOOL])
        reply = await self._stream(request)
        if reply.error is not None and reply.error.get("retryable"):
            # One retry per round for transient provider trouble; usage of both attempts counts.
            await asyncio.sleep(self.retry_backoff_s)
            first = reply
            reply = await self._stream(request)
            reply.usage = _add(first.usage, reply.usage)
        if reply.error is not None:
            raise PlannerError(f"provider error: {reply.error.get('message', 'unknown')}")
        return reply

    async def _stream(self, request: ModelRequest) -> _Reply:
        reply = _Reply()
        texts: list[str] = []
        async for event in self.provider.stream(request, CancelFlag()):
            if event.kind == "text_delta":
                texts.append(str(event.data.get("text", "")))
            elif event.kind == "tool_call":
                reply.calls.append(
                    ToolCall(
                        id=str(event.data["id"]),
                        name=str(event.data["name"]),
                        input=dict(event.data.get("input") or {}),
                    )
                )
            elif event.kind == "usage":
                reply.usage = _add(
                    reply.usage,
                    Usage(
                        input_tokens=int(event.data["input_tokens"]),
                        output_tokens=int(event.data["output_tokens"]),
                    ),
                )
            elif event.kind == "error":
                reply.error = dict(event.data)
        reply.text = "".join(texts)
        return reply


def _describe(error: Any) -> str:
    """A validation error in the tool's own terms (`tasks.0.title`, not the TOML alias)."""
    location = [("tasks" if part == "task" else str(part)) for part in error["loc"]]
    return f"{'.'.join(location) or 'plan'}: {error['msg']}"
