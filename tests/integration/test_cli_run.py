"""`caecilia run/resume/status/runs` against a real repo, and the app functions directly
(backlog M1-12)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from caecilia_harness import bootstrap
from caecilia_harness.bootstrap import (
    AppError,
    build_orchestrator,
    get_run_status,
    list_runs,
    prepare_run,
    prepare_run_from_plan,
    resume_run,
)
from caecilia_harness.core.models import Action, GateReport, Workspace
from caecilia_harness.interfaces.cli import app
from caecilia_harness.paths import harness_home
from caecilia_harness.providers.fake import FakeProvider
from caecilia_harness.store.lock import WriterLock
from caecilia_harness.workers.native.worker import NativeWorker
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
    result = _run(
        git_repo,
        "--worker",
        "fake",
        "--auto-merge",
        "--plain",
        "--max-parallel",
        "3",
        "--provider-slots",
        "3",
        "--exec-slots",
        "4",
    )
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    output = result.output  # type: ignore[attr-defined]
    assert "\x1b" not in output
    assert "task.state_changed" in output and "gate.passed" in output
    assert "[main]" in output
    assert git(git_repo, "show", "main:CAECILIA_FAKE.md") == "tambahkan catatan"

    status = runner.invoke(app, ["status", "--plain"])
    assert status.exit_code == 0, status.output
    assert "DONE" in status.output

    [run] = list_runs()
    options = bootstrap._run_created(run.id)["options"]
    assert options["provider_slots"] == 3
    assert options["exec_slots"] == 4
    runs = runner.invoke(app, ["runs", "--plain"])
    assert runs.exit_code == 0
    assert run.id in runs.output and "done" in runs.output


def test_failing_check_exits_2_needs_human(git_repo: Path) -> None:
    result = _run(git_repo, "--worker", "fake", "--auto-merge", "--plain", check=FAIL)
    assert result.exit_code == 2, result.output  # type: ignore[attr-defined]
    status = get_run_status()
    assert status is not None and status.tasks[0].state == "NEEDS_HUMAN"
    assert status.tasks[0].attempts == 3


def test_native_worker_without_model_or_key_explains_what_is_missing(git_repo: Path) -> None:
    no_model = _run(git_repo, "--worker", "native", "--plain")
    assert no_model.exit_code == 1  # type: ignore[attr-defined]
    assert "--model" in no_model.output  # type: ignore[attr-defined]

    no_key = _run(git_repo, "--worker", "native", "--model", "some-model", "--plain")
    assert no_key.exit_code == 1  # type: ignore[attr-defined]
    assert "caecilia key set deepseek" in no_key.output  # type: ignore[attr-defined]
    assert list_runs() == []  # nothing was created


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


@pytest.mark.parametrize("override", [None, 1, 3])
async def test_parallel_limit_is_saved_and_reused_on_resume(
    git_repo: Path, tmp_path: Path, override: int | None
) -> None:
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text(
        'version = 1\ngoal = "g"\nmax_parallel = 4\n'
        '[[task]]\nid = "api"\ntitle = "API"\ninstructions = "do it"\n',
        encoding="utf-8",
    )
    prepared = await prepare_run_from_plan(
        plan_path,
        git_repo,
        [sys.executable, "-c", "print(1)"],
        _NoTty(),
        worker_kind="fake",
        max_parallel=override,
    )
    run_id = prepared.run_id
    try:
        created = next(e for e in prepared.new_events() if e.type == "run.created")
        assert created.payload["max_parallel"] == (override or 4)
        assert created.payload["options"]["max_parallel"] == (override or 4)
    finally:
        prepared.close()
    resumed = await resume_run(_NoTty(), run_id)
    try:
        assert resumed.orchestrator._max_parallel(run_id) == (override or 4)
    finally:
        resumed.close()


@pytest.mark.parametrize("limit", [0, -1])
async def test_app_rejects_invalid_parallel_limit(git_repo: Path, limit: int) -> None:
    with pytest.raises(AppError, match="max_parallel"):
        await prepare_run(
            "g",
            git_repo,
            [sys.executable, "-c", "print(1)"],
            _NoTty(),
            worker_kind="fake",
            max_parallel=limit,
        )
    assert list_runs() == []


@pytest.mark.parametrize("limit", [None, 3])
async def test_single_task_parallel_limit_default_and_override(
    git_repo: Path, limit: int | None
) -> None:
    prepared = await prepare_run(
        "g",
        git_repo,
        [sys.executable, "-c", "print(1)"],
        _NoTty(),
        worker_kind="fake",
        max_parallel=limit,
    )
    try:
        assert prepared.orchestrator._max_parallel(prepared.run_id) == (limit or 2)
    finally:
        prepared.close()


@pytest.mark.parametrize("from_plan", [False, True])
@pytest.mark.parametrize("limits", [None, (3, 4)])
async def test_slot_limits_are_saved_and_reused_on_resume(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    from_plan: bool,
    limits: tuple[int, int] | None,
) -> None:
    assemble = Mock(wraps=bootstrap._assemble)
    monkeypatch.setattr(bootstrap, "_assemble", assemble)
    options = (
        {} if limits is None else dict(zip(("provider_slots", "exec_slots"), limits, strict=True))
    )
    check = [sys.executable, "-c", "print(1)"]
    if from_plan:
        plan_path = tmp_path / "plan.toml"
        plan_path.write_text(
            'version = 1\ngoal = "g"\n[[task]]\nid = "api"\n'
            'title = "API"\ninstructions = "do it"\n',
            encoding="utf-8",
        )
        prepared = await prepare_run_from_plan(
            plan_path, git_repo, check, _NoTty(), worker_kind="fake", **options
        )
    else:
        prepared = await prepare_run("g", git_repo, check, _NoTty(), worker_kind="fake", **options)
    expected = dict(zip(("provider_slots", "exec_slots"), limits or (2, 2), strict=True))
    try:
        created = next(event for event in prepared.new_events() if event.type == "run.created")
        for name, value in expected.items():
            assert created.payload["options"][name] == value
    finally:
        prepared.close()
    resumed = await resume_run(_NoTty(), prepared.run_id)
    try:
        assert assemble.call_args is not None
        for name, value in expected.items():
            assert assemble.call_args.kwargs[name] == value
    finally:
        resumed.close()


async def test_resume_older_run_defaults_slot_limits(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = await prepare_run("g", git_repo, [sys.executable], _NoTty(), worker_kind="fake")
    try:
        created = next(event for event in prepared.new_events() if event.type == "run.created")
    finally:
        prepared.close()
    payload = created.payload
    payload["options"].pop("provider_slots", None)
    payload["options"].pop("exec_slots", None)
    monkeypatch.setattr(bootstrap, "_run_created", lambda _: payload)
    assemble = Mock(wraps=bootstrap._assemble)
    monkeypatch.setattr(bootstrap, "_assemble", assemble)
    resumed = await resume_run(_NoTty(), prepared.run_id)
    try:
        assert assemble.call_args.kwargs["provider_slots"] == 2
        assert assemble.call_args.kwargs["exec_slots"] == 2
    finally:
        resumed.close()


@pytest.mark.parametrize("name", ["provider_slots", "exec_slots"])
@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "2"])
@pytest.mark.parametrize("entry", ["build", "goal", "plan"])
async def test_app_rejects_invalid_slots_before_opening_database(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    limit: Any,
    entry: str,
) -> None:
    open_db = Mock(side_effect=AssertionError("database must not be opened"))
    monkeypatch.setattr(bootstrap, "open_db", open_db)
    options = {name: limit}
    with pytest.raises(AppError, match=name):
        if entry == "build":
            build_orchestrator(git_repo, "fake", [sys.executable], _NoTty(), **options)
        elif entry == "goal":
            await prepare_run("g", git_repo, [sys.executable], _NoTty(), "fake", **options)
        else:
            plan = tmp_path / "plan.toml"
            plan.write_text(
                'version = 1\ngoal = "g"\n[[task]]\nid = "api"\n'
                'title = "API"\ninstructions = "do it"\n',
                encoding="utf-8",
            )
            await prepare_run_from_plan(
                plan, git_repo, [sys.executable], _NoTty(), "fake", **options
            )
    open_db.assert_not_called()
    assert not (harness_home() / "harness.lock").exists()


async def test_bootstrap_shares_provider_and_exec_semaphores(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider([])
    monkeypatch.setattr(bootstrap, "make_provider", lambda _: provider)
    monkeypatch.setattr(bootstrap, "get_api_key", lambda _: "test-key")
    limited = Mock(wraps=bootstrap.LimitedProvider)
    runners = Mock(wraps=bootstrap.ExecRunner)
    monkeypatch.setattr(bootstrap, "LimitedProvider", limited)
    monkeypatch.setattr(bootstrap, "ExecRunner", runners)
    orchestrator = build_orchestrator(
        git_repo,
        "native",
        [sys.executable],
        _NoTty(),
        model="test-model",
        provider_slots=3,
        exec_slots=4,
    )
    try:
        native = cast(NativeWorker, orchestrator.workers["native"])
        limited.assert_called_once()
        inner, provider_semaphore = limited.call_args.args
        assert inner is provider
        assert isinstance(provider_semaphore, asyncio.Semaphore)
        assert provider_semaphore._value == 3
        for task_id in ("api", "web"):
            native.exec_runner_factory(
                Workspace(task_id=task_id, path=str(git_repo), branch=task_id, base_commit="base")
            )
        first, second = [call.kwargs["slots"] for call in runners.call_args_list]
        assert first is second
        assert first is not provider_semaphore
        assert first._value == 4
    finally:
        orchestrator.close()


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
