"""Graph revision operations as data (backlog M2-06)."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from caecilia_harness.core.models import (
    AddTask,
    CancelTask,
    GraphOp,
    RetryTask,
    SetBlockers,
    TaskDraft,
)

OPS: TypeAdapter[list[GraphOp]] = TypeAdapter(list[GraphOp])


def test_ops_round_trip_through_json_by_their_op_field() -> None:
    ops = [
        AddTask(task=TaskDraft(id="docs", title="Docs", instructions="i"), blocked_by=["api"]),
        SetBlockers(task_id="web", blocked_by=[]),
        CancelTask(task_id="old"),
        RetryTask(task_id="api"),
    ]
    data = OPS.dump_python(ops, mode="json")
    assert [op["op"] for op in data] == ["add_task", "set_blockers", "cancel_task", "retry_task"]
    assert OPS.validate_python(data) == ops


def test_unknown_op_and_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        OPS.validate_python([{"op": "delete_everything", "task_id": "x"}])
    with pytest.raises(ValidationError):
        OPS.validate_python([{"op": "cancel_task", "task_id": "x", "force": True}])
