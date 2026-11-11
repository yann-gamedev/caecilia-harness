"""`caecilia run/resume/status/runs` against a real repo, and the app functions directly
(backlog M1-12)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from caecilia_harness.bootstrap import (
    AppError,
    get_run_status,
    list_runs,
    prepare_run,
    resume_run,
)
from caecilia_harness.core.models import Action, GateReport
from caecilia_harness.interfaces.cli import app
from caecilia_harness.paths import harness_home
from caecilia_harness.store.lock import WriterLock
from tests.integration.conftest import git

runner = CliRunner()


def _cmd(code: str) -> str:
    """A --check string for this platform's parser (Windows keeps backslashes in paths)."""
    return f'"{sys.executable}" -c "{code}"'


PASS = _cmd("print(1)")
FAIL = _cmd("import sys; sys.exit(1)")


def _run(repo: Path, *extra: str, check: str = PASS) -> object:
    return runner.invoke(
        app,
        ["run", "tambahkan catatan", "--repo", str(repo), "--check", check, *extra],
    )


def test_run_fake_auto_merge_then_status_and_runs(git_repo: Path) -> None:
    result = _run(git_repo, "--worker", "fake", "--auto-merge", "--plain")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    output = result.output  # type: ignore[attr-defined]
    assert "\x1b" not in output
    assert "task.state_changed" in output and "gate.passed" in output
    assert git(git_repo, "show", "main:CAECILIA_FAKE.md") == "tambahkan catatan"

    status = runner.invoke(app, ["status", "--plain"])
    assert status.exit_code == 0, status.output
    assert "DONE" in status.output

    [run] = list_runs()
    runs = runner.invoke(app, ["runs", "--plain"])
    assert runs.exit_code == 0
    assert run.id in runs.output and "done" in runs.output


def test_failing_check_exits_2_needs_human(git_repo: Path) -> None:
    result = _run(git_repo, "--worker", "fake", "--auto-merge", "--plain", check=FAIL)
    assert result.exit_code == 2, result.output  # type: ignore[attr-defined]
    status = get_run_status()
    assert status is not None and status.tasks[0].state == "NEEDS_HUMAN"
    assert status.tasks[0].attempts == 3


def test_native_worker_points_to_decision_d2(git_repo: Path) -> None:
    result = _run(git_repo, "--worker", "native", "--plain")
    assert result.exit_code == 1
    assert "D2" in result.output  # type: ignore[attr-defined]


def test_not_a_git_repo(tmp_path: Path) -> None:
    result = _run(tmp_path, "--worker", "fake", "--plain")
    assert result.exit_code == 1
    assert "bukan repo Git" in result.output  # type: ignore[attr-defined]


def test_without_auto_merge_and_no_tty_nothing_is_merged(git_repo: Path) -> None:
    old = git(git_repo, "rev-parse", "main")
    result = _run(git_repo, "--worker", "fake", "--plain")
    assert result.exit_code == 2  # merge not confirmed -> NEEDS_HUMAN
    assert git(git_repo, "rev-parse", "main") == old


def test_status_works_while_writer_lock_is_held(git_repo: Path) -> None:
    assert _run(git_repo, "--worker", "fake", "--auto-merge").exit_code == 0  # type: ignore[attr-defined]
    with WriterLock(harness_home() / "harness.lock"):
        status = runner.invoke(app, ["status", "--plain"])
        runs = runner.invoke(app, ["runs", "--plain"])
    assert status.exit_code == 0 and "DONE" in status.output
    assert runs.exit_code == 0


def test_status_without_runs() -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Belum ada run" in result.output


def test_resume_command_finishes_an_interrupted_run(git_repo: Path) -> None:
    import asyncio

    async def interrupted() -> str:
        prepared = await prepare_run(
            "lanjutkan nanti",
            git_repo,
            [sys.executable, "-c", "print(1)"],
            _NoTty(),
            worker_kind="fake",
            auto_merge=True,
        )
        run_id = prepared.run_id
        prepared.close()  # "crash" before anything ran
        return run_id

    run_id = asyncio.run(interrupted())
    result = runner.invoke(app, ["resume", "--plain"])
    assert result.exit_code == 0, result.output
    assert run_id in result.output
    assert git(git_repo, "show", "main:CAECILIA_FAKE.md") == "lanjutkan nanti"


def test_resume_without_unfinished_run() -> None:
    result = runner.invoke(app, ["resume"])
    assert result.exit_code == 1
    assert "tidak ada run" in result.output


# --- application functions, without the CLI ---------------------------------------------------


class _NoTty:
    async def ask_approval(self, action: Action) -> bool:
        return False

    async def confirm_merge(self, report: GateReport) -> bool:
        return False


async def test_app_functions_directly(git_repo: Path) -> None:
    check = [sys.executable, "-c", "print(1)"]
    prepared = await prepare_run(
        "catatan langsung", git_repo, check, _NoTty(), worker_kind="fake", auto_merge=True
    )
    try:
        assert await prepared.execute() == "done"
        types = [event.type for event in prepared.new_events()]
        assert types[0] == "run.created" and types[-1] == "run.finished"
        assert prepared.new_events() == []  # nothing new since the previous call
    finally:
        prepared.close()

    [run] = list_runs()
    assert (run.id, run.status, run.goal) == (prepared.run_id, "done", "catatan langsung")
    status = get_run_status(run.id)
    assert status is not None
    [task] = status.tasks
    assert (task.state, task.attempts, task.worker) == ("DONE", 1, "fake")

    with pytest.raises(AppError, match="tidak ada run"):
        await resume_run(_NoTty())
    with pytest.raises(AppError):
        get_run_status("unknown")


async def test_resume_run_directly(git_repo: Path) -> None:
    check = [sys.executable, "-c", "print(1)"]
    first = await prepare_run(
        "nanti saja", git_repo, check, _NoTty(), worker_kind="fake", auto_merge=True
    )
    first.close()

    resumed = await resume_run(_NoTty())
    try:
        assert resumed.run_id == first.run_id
        assert await resumed.execute() == "done"
    finally:
        resumed.close()


async def test_prepare_run_with_explicit_target(git_repo: Path) -> None:
    git(git_repo, "branch", "release")
    check = [sys.executable, "-c", "print(1)"]
    prepared = await prepare_run(
        "ke release", git_repo, check, _NoTty(), "fake", target="release", auto_merge=True
    )
    try:
        assert await prepared.execute() == "done"
    finally:
        prepared.close()
    assert git(git_repo, "show", "release:CAECILIA_FAKE.md") == "ke release"
    with pytest.raises(AppError, match="branch target tidak ada"):
        await prepare_run("x", git_repo, check, _NoTty(), "fake", target="nope")
