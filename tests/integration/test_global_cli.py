"""`caecilia` as a global command: repo from the working folder, options from `.caecilia.toml`
(backlog CLI-01, ADR-0015)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from caecilia_harness import __version__, bootstrap
from caecilia_harness.bootstrap import list_runs
from caecilia_harness.core.models import ModelEvent
from caecilia_harness.core.ports import ModelProvider
from caecilia_harness.interfaces.cli import app
from caecilia_harness.project_config import CONFIG_NAME, load_config
from caecilia_harness.providers.fake import FakeProvider
from tests.integration.conftest import git

runner = CliRunner()
# TOML literal strings ('...') keep Windows backslashes as they are.
FAKE_CONFIG = f"version = 1\nworker = \"fake\"\ncheck = ['{sys.executable}', '-c', 'print(1)']\n"


def _config(repo: Path, text: str = FAKE_CONFIG) -> None:
    (repo / CONFIG_NAME).write_text(text, encoding="utf-8")


def _subfolder(repo: Path) -> Path:
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True, exist_ok=True)
    return sub


def test_run_from_a_subfolder_without_repo_or_check(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(git_repo)  # deliberately left uncommitted, as `caecilia init` leaves it
    monkeypatch.chdir(_subfolder(git_repo))

    result = runner.invoke(app, ["run", "catatan dari subfolder", "--auto-merge", "--plain"])

    assert result.exit_code == 0, result.output
    assert git(git_repo, "show", "main:CAECILIA_FAKE.md") == "catatan dari subfolder"
    [run] = list_runs()
    assert run.status == "done"


def test_command_line_wins_over_the_config(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _config(git_repo)
    monkeypatch.chdir(git_repo)
    # --worker native overrides `worker = "fake"`, so a model becomes required.
    result = runner.invoke(app, ["run", "x", "--worker", "native", "--plain"])
    assert result.exit_code == 1
    assert "--model" in result.output and "`model`" in result.output


def test_missing_check_names_option_and_field(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(git_repo, 'version = 1\nworker = "fake"\n')
    monkeypatch.chdir(git_repo)
    result = runner.invoke(app, ["run", "x", "--plain"])
    assert result.exit_code == 1
    assert "--check" in result.output and "`check`" in result.output
    assert list_runs() == []


def test_outside_a_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["run", "x", "--check", "python -V", "--worker", "fake"])
    assert result.exit_code == 1
    assert "bukan bagian dari repo Git" in result.output


def test_invalid_config_is_reported(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _config(git_repo, 'version = 1\napi_key = "sk-123"\n')
    monkeypatch.chdir(git_repo)
    result = runner.invoke(app, ["run", "x", "--plain"])
    assert result.exit_code == 1
    assert "caecilia key set" in result.output
    assert "sk-123" not in result.output


def test_status_and_resume_follow_the_current_repo(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(git_repo)
    monkeypatch.chdir(git_repo)
    assert runner.invoke(app, ["run", "punya repo ini", "--auto-merge"]).exit_code == 0

    other = tmp_path / "other repo"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    monkeypatch.chdir(other)
    elsewhere = runner.invoke(app, ["status", "--plain"])
    assert "Belum ada run" in elsewhere.output  # the other repo's runs are not shown here
    assert "tidak ada run yang belum selesai" in runner.invoke(app, ["resume"]).output

    monkeypatch.chdir(_subfolder(git_repo))
    here = runner.invoke(app, ["status", "--plain"])
    assert here.exit_code == 0 and "DONE" in here.output


def test_plan_takes_its_model_from_the_config(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(git_repo, FAKE_CONFIG + 'model = "cfg-model"\n')
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-CANARY")
    providers: list[FakeProvider] = []

    def scripted(api_key: str) -> ModelProvider:
        task: dict[str, Any] = {
            "id": "notes",
            "title": "Notes",
            "instructions": "Write notes.",
            "acceptance": ["notes exist"],
            "write_set": ["notes/**"],
            "blocked_by": [],
        }
        turn = [
            FakeProvider.tool_call("c1", "submit_plan", {"goal": "g", "tasks": [task]}),
            ModelEvent(kind="done", data={"stop_reason": "tool_use"}),
        ]
        providers.append(FakeProvider([turn]))
        return providers[-1]

    monkeypatch.setattr(bootstrap, "make_provider", scripted)
    monkeypatch.chdir(_subfolder(git_repo))

    result = runner.invoke(app, ["plan", "tulis catatan", "--plain"])

    assert result.exit_code == 0, result.output
    assert providers[0].requests[0].model == "cfg-model"
    assert (Path.cwd() / "plan.toml").is_file()  # relative paths follow the working folder


# --- init -----------------------------------------------------------------------------------------


def test_init_without_a_terminal_uses_the_options(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_subfolder(git_repo))
    result = runner.invoke(
        app, ["init", "--check", "python -m pytest", "--worker", "native", "--model", "m1"]
    )
    assert result.exit_code == 0, result.output

    path = git_repo / CONFIG_NAME  # written at the repo root, not in the subfolder
    config = load_config(path)
    assert (config.check, config.worker, config.model) == ("python -m pytest", "native", "m1")
    assert CONFIG_NAME not in git(git_repo, "status", "--porcelain", "--untracked-files=no")

    again = runner.invoke(app, ["init", "--check", "other", "--worker", "fake"])
    assert again.exit_code == 1 and "--force" in again.output
    assert load_config(path).check == "python -m pytest"

    forced = runner.invoke(app, ["init", "--check", "other", "--worker", "fake", "--force"])
    assert forced.exit_code == 0
    assert load_config(path).worker == "fake"


@pytest.mark.parametrize(
    ("args", "expected"),
    [(["--worker", "fake"], "--check"), (["--check", "x", "--worker", "native"], "--model")],
)
def test_init_without_a_terminal_fails_clearly_when_values_are_missing(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], expected: str
) -> None:
    monkeypatch.chdir(git_repo)
    result = runner.invoke(app, ["init", *args])
    assert result.exit_code == 1
    assert expected in result.output
    assert not (git_repo / CONFIG_NAME).exists()


def test_init_can_stage_the_file_but_never_commits(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(git_repo)
    head = git(git_repo, "rev-parse", "HEAD")
    result = runner.invoke(app, ["init", "--check", "python -V", "--worker", "fake", "--git-add"])
    assert result.exit_code == 0, result.output
    assert git(git_repo, "diff", "--cached", "--name-only") == CONFIG_NAME
    assert git(git_repo, "rev-parse", "HEAD") == head


def test_init_outside_a_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--check", "x", "--worker", "fake"])
    assert result.exit_code == 1 and "bukan bagian dari repo Git" in result.output


# --- version and doctor ----------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flag(flag: str) -> None:
    result = runner.invoke(app, [flag])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_doctor_checks_the_project_config_and_its_key(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(git_repo)
    _config(git_repo, 'version = 1\nworker = "native"\nmodel = "m"\ncheck = "python -V"\n')
    missing_key = runner.invoke(app, ["doctor"])
    lines = {line.split()[1]: line for line in missing_key.output.splitlines() if line.strip()}
    assert lines["config"].startswith("[ok]")
    assert lines["api-key"].startswith("[warn]") and "caecilia key set deepseek" in lines["api-key"]
    assert "path" in lines  # whether `caecilia` itself is on PATH

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-CANARY")
    with_key = runner.invoke(app, ["doctor"])
    assert "sk-test-CANARY" not in with_key.output
    assert any(l.startswith("[ok]") and "api-key" in l for l in with_key.output.splitlines())

    _config(git_repo, "version = 1\nnope = 1\n")
    broken = runner.invoke(app, ["doctor"])
    assert broken.exit_code == 1
    assert any(l.startswith("[FAIL]") and "config" in l for l in broken.output.splitlines())
