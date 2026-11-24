"""`Session`: the flow every frontend shares, tested without a terminal (backlog CLI-02)."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Any

import pytest

from caecilia_harness import bootstrap
from caecilia_harness.bootstrap import AppError
from caecilia_harness.core.models import Action, GateReport, ModelEvent
from caecilia_harness.core.ports import ModelProvider
from caecilia_harness.planning.plan import PlanError, load_plan
from caecilia_harness.project_config import (
    CliValues,
    MissingOptionError,
    ProjectConfig,
    option_sources,
    resolve_options,
)
from caecilia_harness.providers.fake import FakeProvider
from caecilia_harness.session import AllowListPrompter, Session
from tests.integration.conftest import git, git_repo  # noqa: F401 - fixture

CANARY = "sk-test-CANARY-session"
CHECK = [sys.executable, "-c", "print(1)"]


class _Refuse:
    async def ask_approval(self, action: Action) -> bool:
        return False

    async def confirm_merge(self, report: GateReport) -> bool:
        return False


def _session(repo: Path, worker: str = "fake", **options: Any) -> Session:
    cli = CliValues(worker=worker, model=options.pop("model", None))
    resolved = dataclasses.replace(resolve_options(cli, None), check=CHECK)
    return Session(
        repo=repo,
        options=resolved,
        prompter=_Refuse(),
        sources=option_sources(cli, None),
        auto_merge=options.pop("auto_merge", True),
    )


def _task(task_id: str, *blocked_by: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "instructions": f"Do {task_id}.",
        "acceptance": [],
        "write_set": [f"{task_id}/**"],
        "blocked_by": list(blocked_by),
        "idempotent": True,
    }


@pytest.fixture
def planner(monkeypatch: pytest.MonkeyPatch) -> list[FakeProvider]:
    monkeypatch.setenv("DEEPSEEK_API_KEY", CANARY)
    made: list[FakeProvider] = []
    script = [
        [
            FakeProvider.tool_call(
                "c1", "submit_plan", {"goal": "g", "tasks": [_task("api"), _task("web", "api")]}
            ),
            FakeProvider.usage(100, 20),
            ModelEvent(kind="done", data={"stop_reason": "tool_use"}),
        ]
    ]

    def provider(api_key: str) -> ModelProvider:
        assert api_key == CANARY
        made.append(FakeProvider(script))
        return made[-1]

    monkeypatch.setattr(bootstrap, "make_provider", provider)
    return made


async def test_fake_worker_plan_runs_to_done_with_a_report(
    git_repo: Path,  # noqa: F811
) -> None:
    session = _session(git_repo)
    plan = await session.propose("tulis catatan")
    assert [task.id for task in plan.tasks] == ["main"]
    assert session.last_plan is plan
    assert session.last_plan_usage.input_tokens == 0

    prepared = await session.start()
    try:
        assert await prepared.execute() == "done"
    finally:
        prepared.close()

    report = session.report(prepared.run_id)
    assert report.status == "done"
    [outcome] = report.tasks
    assert (outcome.plan_id, outcome.state) == ("main", "DONE")
    assert outcome.merged_commit == git(git_repo, "rev-parse", "main")
    assert report.refused_commands == []
    status = session.status()
    assert status is not None and status.run.id == prepared.run_id
    assert [run.id for run in session.runs()] == [prepared.run_id]
    assert [task.plan_id for task in session.graph().tasks] == ["main"]


async def test_native_plan_comes_from_the_planner_and_writes_no_file(
    git_repo: Path,  # noqa: F811
    planner: list[FakeProvider],
) -> None:
    session = _session(git_repo, worker="native", model="m1")
    plan = await session.propose("endpoint lalu halaman")
    assert [(t.id, t.blocked_by) for t in plan.tasks] == [("api", []), ("web", ["api"])]
    assert session.last_plan_usage.input_tokens == 100
    assert len(planner) == 1
    assert git(git_repo, "status", "--porcelain") == ""  # nothing written into the repo


async def test_native_needs_a_model(git_repo: Path) -> None:  # noqa: F811
    session = _session(git_repo, worker="native")
    with pytest.raises(MissingOptionError, match="--model"):
        await session.propose("x")


async def test_start_needs_a_plan_and_a_check(git_repo: Path) -> None:  # noqa: F811
    session = _session(git_repo)
    with pytest.raises(AppError, match="belum ada plan"):
        await session.start()
    await session.propose("x")
    session.options = dataclasses.replace(session.options, check=None)
    with pytest.raises(MissingOptionError, match="--check"):
        await session.start()


async def test_edited_plan_is_validated_and_a_broken_one_keeps_the_old(
    git_repo: Path,  # noqa: F811
) -> None:
    session = _session(git_repo)
    original = await session.propose("x")
    edited = session.plan_text().replace('id = "main"', 'id = "renamed"')
    assert [t.id for t in session.replace_plan(edited).tasks] == ["renamed"]
    with pytest.raises(PlanError):
        session.replace_plan("version = 1\n[[task]]\nid = 'a'\n")
    assert [t.id for t in session._plan().tasks] == ["renamed"]
    assert original is not session.last_plan


async def test_save_writes_only_on_request_and_refuses_to_overwrite(
    git_repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    session = _session(git_repo)
    with pytest.raises(AppError):
        session.save(tmp_path / "p.toml")
    await session.propose("simpan aku")
    path = session.save(tmp_path / "out" / "p.toml")
    assert load_plan(path).goal == "simpan aku"
    with pytest.raises(AppError, match="sudah ada"):
        session.save(path)
    session.save(path, force=True)
    assert session.load(path).goal == "simpan aku"


async def test_unfinished_run_is_offered_and_resumed(git_repo: Path) -> None:  # noqa: F811
    session = _session(git_repo)
    await session.propose("nanti saja")
    prepared = await session.start()
    prepared.close()  # created but never executed, as after a crash
    [unfinished] = session.unfinished_runs()
    assert unfinished.id == prepared.run_id

    resumed = await session.resume()
    try:
        assert await resumed.execute() == "done"
    finally:
        resumed.close()
    assert session.unfinished_runs() == []


def test_config_view_names_the_origin_and_never_the_key(
    git_repo: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", CANARY)
    cli = CliValues(model="m-cli")
    config = ProjectConfig(worker="native", check="pytest", allow_commands=[["git", "status"]])
    session = Session(
        git_repo, resolve_options(cli, config), _Refuse(), option_sources(cli, config)
    )
    view = {item.name: (item.value, item.source) for item in session.config_view()}
    assert view["model"] == ("m-cli", "cli")
    assert view["worker"] == ("native", "config")
    assert view["check"] == ("pytest", "config")
    assert view["provider"] == ("deepseek", "default")
    assert view["allow_commands"] == ("git status", "config")
    assert CANARY not in repr(session.config_view())
    assert session.key_present() is True
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert session.key_present() is False
    assert _session(git_repo).key_present() is None  # fake worker needs no key


def _exec(*argv: str) -> Action:
    return Action(kind="exec", task_id="t1", detail={"argv": list(argv)})


async def test_allow_list_prompter_never_asks() -> None:
    prompter = AllowListPrompter([["git", "status"], ["uv", "run", "pytest"], []])
    assert await prompter.ask_approval(_exec("git", "status", "--short"))
    assert await prompter.ask_approval(_exec("uv", "run", "pytest", "-q"))
    assert not await prompter.ask_approval(_exec("uv", "run", "ruff"))
    assert not await prompter.ask_approval(_exec("git"))  # shorter than the prefix
    assert not await prompter.ask_approval(_exec())  # an empty prefix allows nothing
    assert prompter.refused == [["uv", "run", "ruff"], ["git"], []]
    report = GateReport(task_id="t1", passed=True)
    assert not await prompter.confirm_merge(report)
