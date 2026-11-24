"""Spawn and artifact failures become failed results, never hanging event streams (EXT-01)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from caecilia_harness import artifacts
from caecilia_harness.core.models import Budget, ContextPack, TaskSpec, Workspace
from caecilia_harness.workers.external.base import ExternalAgentWorker
from caecilia_harness.workers.external.profile import AgentProfile, profile_for
from tests.unit.workers.test_external_worker import _run


async def test_missing_binary_returns_failed_result(tmp_path: Path) -> None:
    profile = AgentProfile(
        name="fake-agent",
        binary=str(tmp_path / "missing"),
        min_version="0.1.0",
        auth="subscription",
    )
    worker = ExternalAgentWorker(profile, env={})
    spec = TaskSpec(id="t", run_id="r", title="demo", instructions="demo")
    ws = Workspace(task_id="t", path=str(tmp_path), branch="t", base_commit="abc")
    handle = await worker.start(spec, ContextPack(task=spec), ws, Budget())
    events = [event async for event in worker.events(handle)]
    result = await worker.result(handle)
    assert not result.success and result.error.startswith("agent_spawn_failed:")
    assert events[-1].data["status"] == "finished"


async def test_log_write_error_finishes_stream_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(data: bytes, kind: str) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(artifacts, "put_bytes", fail)
    script = 'import sys; sys.stdin.read(); print(\'{"event":"result","ok":true}\')'
    _, _, (_, result) = await asyncio.wait_for(_run(tmp_path, script), 15)
    assert not result.success and "worker_log_failed:" in result.error


async def test_parent_exit_finishes_worker_despite_inherited_child_pipes(tmp_path: Path) -> None:
    script = (
        "import subprocess,sys; sys.stdin.read(); "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        'print(\'{"event":"result","ok":true}\',flush=True)'
    )
    _, _, (events, result) = await asyncio.wait_for(_run(tmp_path, script), 15)
    assert result.success, result.error
    assert events[-1].data["status"] == "finished"


def test_doctor_checks_no_run_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from caecilia_harness.workers.external import profile as profiles

    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "claude 1.2.3", "")

    monkeypatch.setattr(profiles, "_which", lambda binary: "claude")
    monkeypatch.setattr(profiles.subprocess, "run", run)
    detection = profiles.detect_agent(profile_for("claude-code", extra_args=("--dangerous",)))
    assert detection.compatible
    assert calls == [["claude", "--version"]]
