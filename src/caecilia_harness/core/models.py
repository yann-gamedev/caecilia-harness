"""Data contracts that cross module or process boundaries.

Keep these small. Add fields when a backlog task needs them, with a test.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

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


class TaskStateChange(Frozen):
    task_id: str
    from_state: TaskState
    to_state: TaskState
    revision: int
    reason: str | None = None


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


class Message(Frozen):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None


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
