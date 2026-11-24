"""Failure and redaction boundaries of the generic external worker (EXT-01)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from caecilia_harness import artifacts
from caecilia_harness.core.models import (
    Budget,
    ContextPack,
    RunHandle,
    TaskSpec,
    WorkerEvent,
    WorkerResult,
    Workspace,
)
from caecilia_harness.security.env import agent_env
from caecilia_harness.workers.external.base import ExternalAgentWorker, parse_line
from caecilia_harness.workers.external.profile import AgentProfile


async def _run(
    tmp_path: Path, script: str
) -> tuple[ExternalAgentWorker, RunHandle, tuple[list[WorkerEvent], WorkerResult]]:
    profile = AgentProfile(
        name="fake-agent",
        binary=sys.executable,
        min_version="0.1.0",
        auth="subscription",
        extra_args=("-u", "-c", script),
    )
    worker = ExternalAgentWorker(profile, env=agent_env(profile, os.environ), grace_s=0.1)
    spec = TaskSpec(id="t", run_id="r", title="demo", instructions="x" * 200000)
    ws = Workspace(task_id="t", path=str(tmp_path), branch="task", base_commit="abc")
    handle = await worker.start(spec, ContextPack(task=spec), ws, Budget())
    events = [event async for event in worker.events(handle)]
    result = await worker.result(handle)
    return worker, handle, (events, result)


@pytest.mark.parametrize("value", [{}, {"ok": "false"}, {"ok": 1}])
def test_missing_or_non_boolean_result_is_rejected(value: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="boolean ok"):
        parse_line(json.dumps({"event": "result", **value}))


@pytest.mark.parametrize("tokens", [-1, True, "10", 1.5])
def test_invalid_usage_is_rejected(tokens: object) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        parse_line(json.dumps({"event": "usage", "input_tokens": tokens}))


async def test_output_and_result_redacted_without_prior_key_lookup(tmp_path: Path) -> None:
    script = (
        "import json,sys; sys.stdin.read(); "
        'print(json.dumps({"event":"tool","input":{"sk-test-CANARY-EXT01":'
        '{"nested":["sk-test-CANARY-EXT01"]}}})); '
        'print(json.dumps({"event":"result","ok":True,"summary":"sk-test-CANARY-EXT01"}))'
    )
    worker, handle, data = await asyncio.wait_for(_run(tmp_path, script), 10)
    events, result = data
    assert result.summary == "***"
    assert "sk-test-CANARY-EXT01" not in str(events)
    digest = worker.log_digest(handle)
    assert digest is not None
    assert b"sk-test-CANARY-EXT01" not in artifacts.read_bytes(digest)


async def test_bad_event_kills_child_and_finishes_events(tmp_path: Path) -> None:
    script = (
        "import sys,time; sys.stdin.read(); "
        'print(\'{"event":"usage","input_tokens":"bad"}\',flush=True); time.sleep(60)'
    )
    _, _, data = await asyncio.wait_for(_run(tmp_path, script), 15)
    events, result = data
    assert not result.success and "worker_crashed" in result.error
    assert events[-1].data["status"] == "finished"


async def test_stdout_is_drained_while_sending_large_stdin(tmp_path: Path) -> None:
    script = (
        'import sys; print("x"*200000,flush=True); sys.stdin.read(); '
        'print(\'{"event":"result","ok":true}\',flush=True)'
    )
    _, _, data = await asyncio.wait_for(_run(tmp_path, script), 15)
    _, result = data
    assert result.success
