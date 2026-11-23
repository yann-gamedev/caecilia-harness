"""Data contracts that cross module or process boundaries.

Keep these small. Add fields when a backlog task needs them, with a test.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from caecilia_harness.core.states import TaskState


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- Events -----------------------------------------------------------------


class Actor(Frozen):
    kind: Literal["user", "orchestrator", "worker", "gate", "plugin"]
    id: str


class NewEvent(Frozen):
    """An event to append. The store assigns `seq`, `stream_seq`, `event_id` and `ts`."""

    stream: str  # e.g. "run:<id>", "task:<id>", "doc:<id>"
    type: str  # e.g. "task.state_changed"
    actor: Actor
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_ref: str | None = None  # artifact hash for large payloads
    causation_id: str | None = None
    correlation_id: str | None = None
    idempotency_key: str | None = None
    schema_version: int = 1


class Event(NewEvent):
    event_id: str
    seq: int
    stream_seq: int
    ts: datetime


# --- Tasks and budgets ------------------------------------------------------


class Budget(Frozen):
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost_usd: float | None = None
    max_steps: int | None = None
    max_wall_seconds: int | None = None


class TaskSpec(Frozen):
    id: str
    run_id: str
    title: str
    instructions: str
    acceptance: list[str] = Field(default_factory=list)
    write_set: list[str] = Field(default_factory=list)  # glob patterns, repo-relative
    blocked_by: list[str] = Field(default_factory=list)
    idempotent: bool = True
    worker: str = "native"
    budget: Budget = Field(default_factory=Budget)


class TaskRow(Frozen):
    """A task as projected in the `tasks` table."""

    id: str
    run_id: str
    state: TaskState
    revision: int
    attempts: int
    spec: TaskSpec


class TaskStateChange(Frozen):
    task_id: str
    from_state: TaskState
    to_state: TaskState
    revision: int
    reason: str | None = None


# --- Graph revisions (ADR-0003, ADR-0013) -----------------------------------
# Task ids in operations are plan ids (without the `<run_id>-` prefix), as people see them.


class TaskDraft(Frozen):
    """A task to add to a run: the same fields as a `[[task]]` in plan.toml.

    Defined in core so revisions do not depend on the `planning` package.
    """

    id: str
    title: str
    instructions: str
    acceptance: list[str] = Field(default_factory=list)
    write_set: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)
    worker: str | None = None
    idempotent: bool = True
    budget: Budget | None = None


class AddTask(Frozen):
    """Add a new PENDING task. Blockers: `task.blocked_by` plus `blocked_by`."""

    op: Literal["add_task"] = "add_task"
    task: TaskDraft
    blocked_by: list[str] = Field(default_factory=list)


class SetBlockers(Frozen):
    """Replace the blockers of a task that has not started (PENDING only)."""

    op: Literal["set_blockers"] = "set_blockers"
    task_id: str
    blocked_by: list[str]


class CancelTask(Frozen):
    """Cancel a task that is not finished and not being worked on or merged right now."""

    op: Literal["cancel_task"] = "cancel_task"
    task_id: str


class RetryTask(Frozen):
    """Send a NEEDS_HUMAN task back to READY after a person fixed the problem."""

    op: Literal["retry_task"] = "retry_task"
    task_id: str


GraphOp = Annotated[AddTask | SetBlockers | CancelTask | RetryTask, Field(discriminator="op")]


# --- Outbox -----------------------------------------------------------------


class OutboxItem(Frozen):
    """A side effect to perform after the state change that requested it has committed."""

    idempotency_key: str
    kind: str  # e.g. "spawn_worker"
    payload: dict[str, Any] = Field(default_factory=dict)


class OutboxRecord(OutboxItem):
    id: int
    status: Literal["pending", "done", "failed", "cancelled"]
    attempts: int


# --- Leases -----------------------------------------------------------------


class Lease(Frozen):
    """Who may run a task right now. Holder = "<hostname>:<pid>:<boot_id>"."""

    task_id: str
    holder: str
    attempt: int
    expires_at: datetime
    pid: int | None = None


# --- Context ----------------------------------------------------------------


class ContextPin(Frozen):
    doc_id: str
    revision: int


class ContextPack(Frozen):
    """Everything a worker may read about the project besides its worktree."""

    task: TaskSpec
    pins: list[ContextPin] = Field(default_factory=list)
    documents: dict[str, str] = Field(default_factory=dict)  # doc_id -> content
    handoff_notes: list[str] = Field(default_factory=list)


# --- Workspaces, workers, gate ------------------------------------------------


class Workspace(Frozen):
    task_id: str
    path: str
    branch: str
    base_commit: str


class DiffArtifact(Frozen):
    task_id: str
    base_commit: str
    head_commit: str  # commit holding the task's changes; equals base_commit if none
    patch_ref: str  # artifact hash of the patch
    files_changed: list[str]
    includes_untracked: bool = True


class WorkerCapabilities(Frozen):
    streams_events: bool
    supports_cancel: bool
    supports_resume: bool
    reports_usage: bool
    edits_files: bool
    needs_network: bool


class RunHandle(Frozen):
    task_id: str
    worker: str
    attempt: int
    pid: int | None = None


class Usage(Frozen):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


class WorkerEvent(Frozen):
    kind: Literal["progress", "tool_call", "tool_result", "approval_needed", "log", "usage"]
    data: dict[str, Any] = Field(default_factory=dict)
    durable: bool = False  # ADR-0011: only durable events are persisted


class WorkerResult(Frozen):
    task_id: str
    success: bool
    summary: str
    usage: Usage | None = None
    error: str | None = None


class GateReport(Frozen):
    task_id: str
    passed: bool
    needs_human: bool = False
    checks: dict[str, bool] = Field(default_factory=dict)
    output_tail: str = ""
    merged_commit: str | None = None
    rescue_branch: str | None = None


# --- Models -----------------------------------------------------------------


class ModelCaps(Frozen):
    supports_tools: bool
    supports_streaming: bool
    reports_usage: bool
    context_window: int
    max_output_tokens: int


class ToolCall(Frozen):
    """A tool invocation requested by the model."""

    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class Message(Frozen):
    """A chat message. `assistant` messages may carry `tool_calls`; each `tool` message answers
    one of them via `tool_call_id`."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


class ToolSpec(Frozen):
    name: str
    description: str
    input_schema: dict[str, Any]


class ModelRequest(Frozen):
    model: str
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list)
    max_output_tokens: int | None = None


class ModelEvent(Frozen):
    """One streamed provider event (docs/architecture.md, "Kontrak `ModelEvent`").

    Shape of `data` per `kind`:

    - `text_delta`: `{"text": str}`
    - `tool_call`: `{"id": str, "name": str, "input": dict}`
    - `usage`: `{"input_tokens": int, "output_tokens": int}`
    - `done`: `{"stop_reason": "end_turn" | "tool_use" | "max_tokens" | "cancelled"}`
    - `error`: `{"message": str, "retryable": bool}`

    A stream always ends with exactly one `done` or `error` event.
    """

    kind: Literal["text_delta", "tool_call", "usage", "done", "error"]
    data: dict[str, Any] = Field(default_factory=dict)


# --- Policy -----------------------------------------------------------------


class Action(Frozen):
    kind: Literal["exec", "write_file", "network", "merge", "graph_change", "mcp_tool"]
    task_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class Principal(Frozen):
    kind: Literal["user", "worker", "plugin"]
    id: str


Decision = Literal["allow", "deny", "ask"]
