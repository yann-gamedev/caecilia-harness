"""Harness for the chaos suite: real `caecilia` processes, killed at a held point."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from caecilia_harness.core.orchestrator import HOLD_ENV
from tests.integration.conftest import git, git_repo  # noqa: F401 - re-exported fixture

CLI = [sys.executable, "-m", "caecilia_harness.interfaces.cli"]
#: Upper bound for one subprocess phase; a hang fails the test instead of blocking the suite.
PHASE_TIMEOUT_S = 120


@dataclass
class Chaos:
    repo: Path
    spawn_log: Path
    #: Extra environment for every process of the scenario (e.g. CAECILIA_FAKE_DELAYS).
    extra_env: dict[str, str] = field(default_factory=dict)

    def env(self, hold_at: str | None = None) -> dict[str, str]:
        env = dict(os.environ)  # includes the per-test CAECILIA_HOME from `isolated_home`
        env.update(self.extra_env)
        env["CAECILIA_FAKE_SPAWN_LOG"] = str(self.spawn_log)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop(HOLD_ENV, None)
        if hold_at is not None:
            env[HOLD_ENV] = hold_at
        return env

    def run_argv(self) -> list[str]:
        check = f'"{sys.executable}" -c "print(1)"'
        return [
            *CLI,
            "run",
            "tulis catatan chaos",
            "--repo",
            str(self.repo),
            "--check",
            check,
            "--worker",
            "fake",
            "--auto-merge",
            "--plain",
        ]

    async def start_and_kill_at(self, hold_at: str, argv: list[str] | None = None) -> str:
        """Start a run, wait for `CAECILIA_HOLD <hold_at>`, kill the process hard.

        Returns everything the process printed before it died.
        """
        proc = await asyncio.create_subprocess_exec(
            *(argv or self.run_argv()),
            env=self.env(hold_at),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        seen: list[str] = []
        try:
            async with asyncio.timeout(PHASE_TIMEOUT_S):
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        output = "".join(seen)
                        pytest.fail(f"process ended before holding at {hold_at}:\n{output}")
                    line = raw.decode("utf-8", errors="replace")
                    seen.append(line)
                    if line.startswith(f"CAECILIA_HOLD {hold_at}"):
                        break
        finally:
            if proc.returncode is None:
                proc.kill()  # SIGKILL / TerminateProcess: no cleanup code runs
            await proc.wait()
        return "".join(seen)

    def plan_argv(self, plan: Path, max_parallel: int = 2) -> list[str]:
        """`caecilia run --plan` with the fake worker, auto-merge and a check that passes."""
        check = f'"{sys.executable}" -c "print(1)"'
        return [
            *CLI,
            "run",
            "--plan",
            str(plan),
            "--repo",
            str(self.repo),
            "--check",
            check,
            "--worker",
            "fake",
            "--auto-merge",
            "--max-parallel",
            str(max_parallel),
            "--plain",
        ]

    async def resume(self, run_id: str | None = None) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *CLI,
            "resume",
            *(["--run", run_id] if run_id else []),
            "--plain",
            env=self.env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            async with asyncio.timeout(PHASE_TIMEOUT_S):
                out, _ = await proc.communicate()
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        assert proc.returncode is not None
        return proc.returncode, out.decode("utf-8", errors="replace")

    def spawns(self) -> list[str]:
        if not self.spawn_log.exists():
            return []
        return self.spawn_log.read_text(encoding="utf-8").splitlines()

    def task_commits(self, branch: str = "main") -> list[str]:
        subjects = git(self.repo, "log", "--format=%s", branch).splitlines()
        return [s for s in subjects if s.startswith("caecilia: task")]


@pytest.fixture
def chaos(git_repo: Path, tmp_path: Path) -> Chaos:  # noqa: F811 - the imported fixture
    return Chaos(repo=git_repo, spawn_log=tmp_path / "spawn.log")


def write_plan(folder: Path, tasks: list[tuple[str, list[str]]], extra: str = "") -> Path:
    """A plan.toml with one task per (plan id, blockers); each task writes only `<id>/**`."""
    lines = ["version = 1", 'goal = "chaos paralel"', extra]
    for task_id, blocked_by in tasks:
        blockers = ", ".join(f'"{b}"' for b in blocked_by)
        lines += [
            "",
            "[[task]]",
            f'id = "{task_id}"',
            f'title = "task {task_id}"',
            f'instructions = "tulis {task_id}"',
            f'write_set = ["{task_id}/**"]',
            f"blocked_by = [{blockers}]",
        ]
    path = folder / "plan.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
