"""`plan.toml`: a run with several tasks and their dependencies (ADR-0013, docs/architecture.md).

Reading uses `tomllib` from the standard library. Writing uses a small serializer for this schema
only, so no TOML dependency is needed.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from caecilia_harness.core.globs import WHOLE_REPO, normalize_write_set
from caecilia_harness.core.graph import GraphNode, validate_graph
from caecilia_harness.core.models import Budget, TaskSpec

SINGLE_TASK_ID = "main"
_TITLE_MAX = 80


class PlanError(ValueError):
    """The plan cannot be used; `problems` lists every reason found."""

    def __init__(self, problems: Sequence[str], source: str = "plan") -> None:
        self.problems = list(problems)
        self.source = source
        super().__init__(f"{source}: " + "; ".join(self.problems))


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class PlanBudget(_Strict):
    """Same fields as `core.models.Budget`."""

    max_input_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_steps: int | None = Field(default=None, ge=1)
    max_wall_seconds: int | None = Field(default=None, ge=1)


class PlanTask(_Strict):
    id: str
    title: str
    instructions: str
    acceptance: list[str] = Field(default_factory=list)
    write_set: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)
    worker: str | None = None
    idempotent: bool = True
    budget: PlanBudget | None = None


class Plan(_Strict):
    version: Literal[1]
    goal: str
    target: str | None = None
    max_parallel: int | None = Field(default=None, ge=1)
    budget: PlanBudget | None = None
    # Written as `[[task]]` in TOML.
    tasks: list[PlanTask] = Field(alias="task", min_length=1)


# --- reading --------------------------------------------------------------------------------------


def _validation_problems(exc: ValidationError) -> list[str]:
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "plan"
        problems.append(f"{location}: {error['msg']}")
    return problems


def parse_plan(text: str, source: str = "plan") -> Plan:
    """Parse and fully validate plan text. Raises `PlanError` listing every problem."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        # tomllib's message already carries "(at line N, column M)".
        raise PlanError([f"TOML tidak valid: {exc}"], source) from exc
    try:
        plan = Plan.model_validate(data)
    except ValidationError as exc:
        raise PlanError(_validation_problems(exc), source) from exc
    issues = validate_graph(
        [
            GraphNode(
                id=task.id, blocked_by=tuple(task.blocked_by), write_set=tuple(task.write_set)
            )
            for task in plan.tasks
        ]
    )
    if issues:
        raise PlanError([f"task {i.task_id}: {i.kind}: {i.detail}" for i in issues], source)
    return plan


def load_task_file(path: Path) -> PlanTask:
    """Read a file holding exactly one `[[task]]` in plan format (for `caecilia graph add`).

    Only the task itself is checked here; how it fits the run's graph is checked by the
    revision.
    """
    source = str(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PlanError([f"tidak bisa membaca file: {exc.strerror or exc}"], source) from exc
    except tomllib.TOMLDecodeError as exc:
        raise PlanError([f"TOML tidak valid: {exc}"], source) from exc
    tasks = data.get("task")
    extra = sorted(set(data) - {"task"})
    if extra or not isinstance(tasks, list) or len(tasks) != 1:
        problems = [f"field tidak dikenal: {', '.join(extra)}"] if extra else []
        problems.append("file harus berisi tepat satu [[task]]")
        raise PlanError(problems, source)
    try:
        return PlanTask.model_validate(tasks[0])
    except ValidationError as exc:
        raise PlanError([f"task.{p}" for p in _validation_problems(exc)], source) from exc


def load_plan(path: Path) -> Plan:
    """Read and validate a `plan.toml` file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError([f"tidak bisa membaca file: {exc.strerror or exc}"], str(path)) from exc
    return parse_plan(text, str(path))


# --- writing --------------------------------------------------------------------------------------

_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _escape(text: str, keep: str = "") -> str:
    out = []
    for char in text:
        if char in keep:
            out.append(char)
        elif char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return "".join(out)


def _string(text: str) -> str:
    if "\n" in text:
        # Multi-line basic string: newlines and tabs stay readable, every quote is escaped so a
        # run of three can never close the string early. A newline right after the opening
        # quotes is dropped by TOML, so the content is preserved exactly.
        return '"""\n' + _escape(text, keep="\n\t") + '"""'
    return '"' + _escape(text) + '"'


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} to a plan")


def _budget_lines(budget: PlanBudget) -> list[str]:
    return [
        f"{key} = {_value(value)}"
        for key, value in budget.model_dump().items()
        if value is not None
    ]


def dump_plan(plan: Plan) -> str:
    """Serialize a plan as TOML that `parse_plan` reads back to an equal `Plan`."""
    lines = ["version = 1", f"goal = {_string(plan.goal)}"]
    if plan.target is not None:
        lines.append(f"target = {_string(plan.target)}")
    if plan.max_parallel is not None:
        lines.append(f"max_parallel = {plan.max_parallel}")
    if plan.budget is not None:
        lines += ["", "[budget]", *_budget_lines(plan.budget)]
    for task in plan.tasks:
        lines += [
            "",
            "[[task]]",
            f"id = {_string(task.id)}",
            f"title = {_string(task.title)}",
            f"instructions = {_string(task.instructions)}",
            f"acceptance = {_value(task.acceptance)}",
            f"write_set = {_value(task.write_set)}",
            f"blocked_by = {_value(task.blocked_by)}",
        ]
        if task.worker is not None:
            lines.append(f"worker = {_string(task.worker)}")
        lines.append(f"idempotent = {_value(task.idempotent)}")
        if task.budget is not None:
            lines += ["", "[task.budget]", *_budget_lines(task.budget)]
    return "\n".join(lines) + "\n"


# --- conversion -----------------------------------------------------------------------------------


def _budget(budget: PlanBudget | None) -> Budget:
    return Budget(**budget.model_dump()) if budget is not None else Budget()


def run_budget(plan: Plan) -> Budget | None:
    return _budget(plan.budget) if plan.budget is not None else None


def plan_to_specs(plan: Plan, run_id: str, default_worker: str) -> list[TaskSpec]:
    """Task specs for a run: ids become `<run_id>-<plan id>`, blockers likewise."""
    return [
        TaskSpec(
            id=f"{run_id}-{task.id}",
            run_id=run_id,
            title=task.title,
            instructions=task.instructions,
            acceptance=list(task.acceptance),
            write_set=normalize_write_set(task.write_set),
            blocked_by=[f"{run_id}-{blocker}" for blocker in task.blocked_by],
            idempotent=task.idempotent,
            worker=task.worker or default_worker,
            budget=_budget(task.budget),
        )
        for task in plan.tasks
    ]


def plan_id(task_id: str, run_id: str) -> str:
    """The id a task has in its plan (`<run_id>-` prefix removed)."""
    prefix = f"{run_id}-"
    return task_id.removeprefix(prefix)


def single_task_plan(goal: str, idempotent: bool = True) -> Plan:
    """The plan behind `caecilia run GOAL`: one task that may touch the whole repo."""
    title = goal if len(goal) <= _TITLE_MAX else goal[: _TITLE_MAX - 3] + "..."
    task = PlanTask(
        id=SINGLE_TASK_ID,
        title=title,
        instructions=goal,
        write_set=[WHOLE_REPO],
        idempotent=idempotent,
    )
    return Plan(version=1, goal=goal, tasks=[task])
