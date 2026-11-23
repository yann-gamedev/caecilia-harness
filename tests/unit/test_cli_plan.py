"""`caecilia plan GOAL`: the planner through the CLI, with a scripted provider (backlog M2-05)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from caecilia_harness import bootstrap
from caecilia_harness.core.models import ModelEvent
from caecilia_harness.core.ports import ModelProvider
from caecilia_harness.interfaces.cli import app
from caecilia_harness.paths import harness_home
from caecilia_harness.planning.plan import load_plan
from caecilia_harness.providers.fake import FakeProvider
from tests.integration.conftest import git_repo  # noqa: F401 - fixture

CANARY = "sk-test-CANARY"
GOAL = "tambah endpoint health dan halaman status"
runner = CliRunner()


def _task(task_id: str, *blocked_by: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "instructions": f"Do {task_id}.\nWith a second line.",
        "acceptance": [f"{task_id} works"],
        "write_set": [f"{task_id}/**"],
        "blocked_by": list(blocked_by),
        "idempotent": True,
    }


def _submit(*tasks: dict[str, Any]) -> list[ModelEvent]:
    return [
        FakeProvider.tool_call("c1", "submit_plan", {"goal": GOAL, "tasks": list(tasks)}),
        FakeProvider.usage(321, 45),
        ModelEvent(kind="done", data={"stop_reason": "tool_use"}),
    ]


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> list[FakeProvider]:
    """Replace the real provider with a script; the API key path itself stays real."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", CANARY)
    made: list[FakeProvider] = []
    script: list[list[ModelEvent]] = [_submit(_task("api"), _task("web", "api"))]

    def provider(api_key: str) -> ModelProvider:
        assert api_key == CANARY
        made.append(FakeProvider(script))
        return made[-1]

    monkeypatch.setattr(bootstrap, "make_provider", provider)
    return made


def _plan(repo: Path, out: Path, *extra: str) -> Any:
    args = ["plan", GOAL, "--repo", str(repo), "--model", "m1", "--out", str(out), "--plain"]
    return runner.invoke(app, [*args, *extra])


def test_plan_writes_a_valid_file_and_prints_a_summary(
    git_repo: Path,  # noqa: F811 - the imported fixture
    tmp_path: Path,
    scripted: list[FakeProvider],
) -> None:
    out = tmp_path / "plans" / "plan.toml"
    result = _plan(git_repo, out)

    assert result.exit_code == 0, result.output
    plan = load_plan(out)
    assert plan.goal == GOAL
    assert [(t.id, t.blocked_by) for t in plan.tasks] == [("api", []), ("web", ["api"])]
    assert plan.tasks[0].instructions == "Do api.\nWith a second line."

    validated = runner.invoke(app, ["plan", "validate", str(out)])
    assert validated.exit_code == 0, validated.output

    lines = result.output.splitlines()
    assert any(line.startswith("TASK") and "WRITE_SET" in line for line in lines)
    web = next(line for line in lines if line.startswith("web "))
    assert " api " in web and "web/**" in web
    assert "1 ronde" in result.output and "in 321 / out 45" in result.output
    assert f"caecilia run --plan {out}" in result.output

    [provider] = scripted
    [request] = provider.requests
    assert request.model == "m1"
    assert "Tracked files" in request.messages[1].content  # the repo summary was sent


def test_refuses_to_overwrite_without_force(
    git_repo: Path,  # noqa: F811 - the imported fixture
    tmp_path: Path,
    scripted: list[FakeProvider],
) -> None:
    out = tmp_path / "plan.toml"
    out.write_text("# my own plan\n", encoding="utf-8")

    refused = _plan(git_repo, out)
    assert refused.exit_code == 1
    assert "--force" in refused.output
    assert out.read_text(encoding="utf-8") == "# my own plan\n"
    assert scripted == []  # the model was never asked

    forced = _plan(git_repo, out, "--force")
    assert forced.exit_code == 0, forced.output
    assert len(load_plan(out).tasks) == 2


def test_api_key_never_appears_in_output_or_file(
    git_repo: Path,  # noqa: F811 - the imported fixture
    tmp_path: Path,
    scripted: list[FakeProvider],
) -> None:
    out = tmp_path / "plan.toml"
    result = _plan(git_repo, out)
    assert result.exit_code == 0, result.output
    assert CANARY not in result.output
    assert CANARY not in out.read_text(encoding="utf-8")
    home = harness_home()
    assert not home.exists() or not [p for p in home.rglob("*") if p.is_file()]  # no DB written


def test_planner_failure_lists_the_problems(
    git_repo: Path,  # noqa: F811 - the imported fixture
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", CANARY)
    cyclic = _submit(_task("a", "b"), _task("b", "a"))
    monkeypatch.setattr(bootstrap, "make_provider", lambda key: FakeProvider([cyclic] * 3))
    out = tmp_path / "plan.toml"

    result = _plan(git_repo, out)

    assert result.exit_code == 1
    assert "no valid plan after 3 rounds" in result.output
    assert "a -> b -> a" in result.output
    assert not out.exists()


def test_provider_error_message_is_redacted(
    git_repo: Path,  # noqa: F811 - the imported fixture
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", CANARY)
    leaky = [
        ModelEvent(kind="error", data={"message": f"HTTP 401 for {CANARY}", "retryable": False})
    ]
    monkeypatch.setattr(bootstrap, "make_provider", lambda key: FakeProvider([leaky]))

    result = _plan(git_repo, tmp_path / "plan.toml")

    assert result.exit_code == 1
    assert "HTTP 401" in result.output
    assert CANARY not in result.output


def test_missing_key_explains_how_to_add_it(
    git_repo: Path,  # noqa: F811 - the imported fixture
    tmp_path: Path,
) -> None:
    result = _plan(git_repo, tmp_path / "plan.toml")
    assert result.exit_code == 1
    assert "caecilia key set deepseek" in result.output


def test_not_a_git_repo(tmp_path: Path, scripted: list[FakeProvider]) -> None:
    result = _plan(tmp_path, tmp_path / "plan.toml")
    assert result.exit_code == 1
    assert "bukan repo Git" in result.output
    assert scripted == []


def test_plan_group_help_lists_both_commands() -> None:
    result = runner.invoke(app, ["plan", "--help"])
    assert result.exit_code == 0
    assert "create" in result.output and "validate" in result.output
