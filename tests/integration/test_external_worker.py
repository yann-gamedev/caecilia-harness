"""Fake agent reaches the same worktree, write-set check and gate as other workers (EXT-01)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from caecilia_harness import artifacts, bootstrap
from caecilia_harness.bootstrap import AppError, get_run_status, list_runs, make_worker
from caecilia_harness.interfaces.cli import app
from caecilia_harness.paths import db_path
from caecilia_harness.project_config import AgentConfig
from caecilia_harness.store.db import open_readonly
from caecilia_harness.store.events import SqliteEventStore
from tests.integration.conftest import git

runner = CliRunner()
CHECK = f'"{sys.executable}" -c "print(1)"'


@pytest.mark.parametrize("entry", ["run", "print", "plan"])
def test_fake_agent_merges_through_the_gate(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("FAKE_AGENT_ECHO", "sk-test-PERSISTED-CANARY")
    (git_repo / ".caecilia.toml").write_text(
        'worker = "fake-agent"\n[agents.fake-agent]\nauth = "subscription"\n', encoding="utf-8"
    )
    git(git_repo, "add", ".caecilia.toml")
    git(git_repo, "commit", "-qm", "config")
    if entry == "plan":
        plan = git_repo.parent / "plan.toml"
        plan.write_text(
            'version = 1\ngoal = "demo"\n[[task]]\nid = "a"\ntitle = "demo"\n'
            'instructions = "demo"\nwrite_set = ["FAKE_AGENT.md"]\n',
            encoding="utf-8",
        )
        args = ["run", "--plan", str(plan)]
    else:
        args = ["-p" if entry == "print" else "run", "catatan"]
    result = runner.invoke(app, [*args, "--check", CHECK, "--auto-merge", "--plain"])
    assert result.exit_code == 0, result.output
    assert "fake agent" in git(git_repo, "show", "main:FAKE_AGENT.md")
    assert "gate.passed" in result.output
    status = get_run_status()
    assert status is not None and status.tasks[0].state == "DONE"
    store = SqliteEventStore(open_readonly(db_path()))
    try:
        logs = [
            event
            for event in store.read_stream(f"task:{status.tasks[0].id}")
            if event.type == "worker.log"
        ]
        assert len(logs) == 1
        digest = logs[0].payload["log_ref"]
        assert artifacts.artifact_path(digest).is_file()
        log = artifacts.read_bytes(digest)
        assert b'"event": "result"' in log
        assert b"sk-test-PERSISTED-CANARY" not in log
        assert b"***" in log
    finally:
        store.close()
    [run] = list_runs()
    assert (
        bootstrap._run_created(run.id)["options"]["agents"]["fake-agent"]["auth"] == "subscription"
    )


def test_agent_edit_outside_write_set_is_not_merged(git_repo: Path) -> None:
    plan = git_repo.parent / "restricted.toml"
    plan.write_text(
        'version = 1\ngoal = "demo"\n[[task]]\nid = "a"\ntitle = "demo"\n'
        'instructions = "demo"\nwrite_set = ["allowed.txt"]\n',
        encoding="utf-8",
    )
    head = git(git_repo, "rev-parse", "main")
    result = runner.invoke(
        app,
        [
            "run",
            "--repo",
            str(git_repo),
            "--plan",
            str(plan),
            "--worker",
            "fake-agent",
            "--check",
            CHECK,
            "--auto-merge",
        ],
    )
    assert result.exit_code == 2, result.output
    assert git(git_repo, "rev-parse", "main") == head
    status = get_run_status()
    assert status is not None and status.tasks[0].state == "NEEDS_HUMAN"


def test_fake_agent_api_key_is_refused_without_keyerror(git_repo: Path) -> None:
    with pytest.raises(AppError, match="tidak mendukung auth"):
        make_worker("fake-agent", agents={"fake-agent": AgentConfig(auth="api-key")})
    (git_repo / ".caecilia.toml").write_text(
        'worker = "fake-agent"\n[agents.fake-agent]\nauth = "api-key"\n', encoding="utf-8"
    )
    result = runner.invoke(app, ["run", "demo", "--repo", str(git_repo), "--check", CHECK])
    assert result.exit_code == 1
    assert "tidak mendukung auth" in result.output


def test_relative_fixture_override_runs_in_a_different_worktree(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from caecilia_harness.workers.external.profile import fake_agent_script

    script = tmp_path / "fake.py"
    script.write_bytes(fake_agent_script().read_bytes())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CAECILIA_FAKE_AGENT", "fake.py")
    result = runner.invoke(
        app,
        [
            "run",
            "demo",
            "--repo",
            str(git_repo),
            "--worker",
            "fake-agent",
            "--check",
            CHECK,
            "--auto-merge",
        ],
    )
    assert result.exit_code == 0, result.output


def test_real_adapters_are_not_run_before_prompt_37() -> None:
    with pytest.raises(AppError, match="belum tersedia"):
        make_worker("claude-code")


async def test_resume_retains_agent_config(git_repo: Path) -> None:
    class Prompter:
        async def ask_approval(self, action: object) -> bool:
            return False

        async def confirm_merge(self, report: object) -> bool:
            return True

    prepared = await bootstrap.prepare_run(
        "demo",
        git_repo,
        [sys.executable, "-c", "print(1)"],
        Prompter(),
        worker_kind="fake-agent",
        auto_merge=True,
        agents={"fake-agent": AgentConfig(auth="subscription", model="demo")},
    )
    run_id = prepared.run_id
    prepared.close()
    resumed = await bootstrap.resume_run(Prompter(), run_id)
    try:
        assert await resumed.execute() == "done"
    finally:
        resumed.close()
    assert "fake agent" in git(git_repo, "show", "main:FAKE_AGENT.md")
