"""`caecilia -p`: plan, run and summary without any question (backlog CLI-02)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from caecilia_harness import bootstrap
from caecilia_harness.bootstrap import list_runs
from caecilia_harness.core.models import ModelEvent
from caecilia_harness.interfaces.cli import app
from caecilia_harness.providers.fake import FakeProvider
from tests.integration.conftest import git

runner = CliRunner()
CANARY = "sk-test-CANARY-print"
PY = sys.executable


def _cmd(code: str) -> str:
    return f'"{PY}" -c "{code}"'


PASS = _cmd("print(1)")
FAIL = _cmd("import sys; sys.exit(1)")


@pytest.fixture
def in_repo(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(git_repo)
    return git_repo


def _p(*args: str) -> Any:
    return runner.invoke(app, ["-p", *args])


def test_done_exits_0_and_merges_with_auto_merge(in_repo: Path) -> None:
    result = _p("tulis catatan", "--worker", "fake", "--check", PASS, "--auto-merge", "--plain")
    assert result.exit_code == 0, result.output
    assert "TASK" in result.output  # the plan is printed before the run
    assert "Ringkasan run" in result.output and "merge " in result.output
    assert git(in_repo, "show", "main:CAECILIA_FAKE.md") == "tulis catatan"


def test_failing_check_exits_2(in_repo: Path) -> None:
    result = _p("gagal", "--worker", "fake", "--check", FAIL, "--auto-merge", "--plain")
    assert result.exit_code == 2, result.output
    assert "NEEDS_HUMAN" in result.output


def test_without_auto_merge_nothing_is_merged(in_repo: Path) -> None:
    before = git(in_repo, "rev-parse", "main")
    result = _p("jangan merge", "--worker", "fake", "--check", PASS, "--quiet")
    assert result.exit_code == 2, result.output
    assert git(in_repo, "rev-parse", "main") == before
    assert "TASK" not in result.output  # --quiet: no plan
    assert "tidak di-merge" in result.output


def test_json_output_is_one_valid_object(in_repo: Path) -> None:
    result = _p(
        "json", "--worker", "fake", "--check", PASS, "--auto-merge", "--output-format", "json"
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["status"] == "done" and data["exit_code"] == 0
    [task] = data["tasks"]
    assert task["id"] == "main" and task["state"] == "DONE"
    assert task["merged_commit"] == git(in_repo, "rev-parse", "main")
    assert data["tokens"] == {"input": 0, "output": 0}
    assert data["refused_commands"] == []


def test_errors_exit_1_also_as_json(tmp_path: Path) -> None:
    text = _p("di luar repo", "--worker", "fake", "--check", PASS)
    assert text.exit_code == 1 and "bukan repo Git" in text.output
    as_json = _p("di luar repo", "--worker", "fake", "--check", PASS, "--output-format", "json")
    assert as_json.exit_code == 1
    assert json.loads(as_json.stdout)["status"] == "error"


def test_goal_and_plan_are_exclusive(in_repo: Path) -> None:
    result = _p("goal", "--plan", "plan.toml", "--worker", "fake", "--check", PASS)
    assert result.exit_code == 1 and "tepat satu" in result.output
    bare = _p("--worker", "fake", "--check", PASS)
    assert bare.exit_code == 1 and "tepat satu" in bare.output


def test_plan_file_runs_without_the_planner(in_repo: Path, tmp_path: Path) -> None:
    plan = tmp_path / "plan.toml"
    plan.write_text(
        'version = 1\ngoal = "dua task"\n'
        '[[task]]\nid = "a"\ntitle = "A"\ninstructions = "a"\nwrite_set = ["a/**"]\n'
        '[[task]]\nid = "b"\ntitle = "B"\ninstructions = "b"\nwrite_set = ["b/**"]\n'
        'blocked_by = ["a"]\n',
        encoding="utf-8",
    )
    result = _p("--plan", str(plan), "--worker", "fake", "--check", PASS, "--auto-merge", "--plain")
    assert result.exit_code == 0, result.output
    assert [run.goal for run in list_runs()] == ["dua task"]


def _tool_turn(*argvs: list[str]) -> list[ModelEvent]:
    calls = [FakeProvider.tool_call(f"c{i}", "run", {"argv": argv}) for i, argv in enumerate(argvs)]
    return [*calls, ModelEvent(kind="done", data={"stop_reason": "tool_use"})]


def _summary() -> list[ModelEvent]:
    return [
        FakeProvider.text("Selesai."),
        FakeProvider.usage(30, 7),
        ModelEvent(kind="done", data={"stop_reason": "end_turn"}),
    ]


def test_only_allow_listed_commands_run_and_refusals_are_recorded(
    in_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    by_flag = [PY, "-c", "print(1)"]
    by_config = [PY, "-c", "print(3)"]
    refused = [PY, "-c", "print(2)"]
    (in_repo / ".caecilia.toml").write_text(
        f"version = 1\nallow_commands = [{json.dumps(by_config)}]\n", encoding="utf-8"
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", CANARY)
    script = [_tool_turn(by_flag, refused, by_config), _summary()]
    monkeypatch.setattr(bootstrap, "make_provider", lambda key: FakeProvider(script))
    plan = tmp_path / "plan.toml"
    plan.write_text(
        'version = 1\ngoal = "exec"\n[[task]]\nid = "x"\ntitle = "X"\ninstructions = "x"\n',
        encoding="utf-8",
    )
    result = _p(
        "--plan", str(plan), "--worker", "native", "--model", "m1", "--check", PASS,
        "--allow-cmd", PASS, "--output-format", "json",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["refused_commands"] == [refused]
    assert data["tokens"] == {"input": 30, "output": 7}
    assert CANARY not in result.output


def test_print_mode_never_waits_for_input(git_repo: Path, tmp_path: Path) -> None:
    """A real process with stdin closed: no merge confirmation is awaited (exit 2, not a hang)."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    code = "from caecilia_harness.interfaces.cli import app; app()"
    args = ["-p", "tanpa input", "--worker", "fake", "--check", PASS, "--plain"]
    done = subprocess.run(
        [PY, "-c", code, *args],
        cwd=git_repo,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env=env,
    )
    assert done.returncode == 2, done.stdout + done.stderr
    assert "Ringkasan run" in done.stdout
