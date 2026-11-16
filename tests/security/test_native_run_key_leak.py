"""A full `--worker native` run with a recorded transport never leaks the API key
(backlog M1-14, ADR-0007).

The canary key is given through the environment, the provider replays recorded streams, and
afterwards everything the run produced is scanned: CLI output, the database, artifacts,
worktrees and every other file under CAECILIA_HOME.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from caecilia_harness import bootstrap
from caecilia_harness.bootstrap import get_run_status
from caecilia_harness.core.ports import ModelProvider
from caecilia_harness.interfaces.cli import app
from caecilia_harness.paths import harness_home
from caecilia_harness.providers.deepseek import DeepSeekProvider
from tests.contract.deepseek_replay import Replay, client
from tests.integration.conftest import git, git_repo  # noqa: F401 - fixture

CANARY = "sk-test-CANARY"
runner = CliRunner()


def _files_containing(root: Path, needle: bytes) -> list[str]:
    return [str(path) for path in root.rglob("*") if path.is_file() and needle in path.read_bytes()]


def _run_native(repo: Path, monkeypatch: pytest.MonkeyPatch, replay: Replay) -> object:
    monkeypatch.setenv("DEEPSEEK_API_KEY", CANARY)

    def recorded_provider(api_key: str) -> ModelProvider:
        assert api_key == CANARY  # the real key path is exercised, only the transport is fake
        return DeepSeekProvider(api_key, client=client(replay))

    monkeypatch.setattr(bootstrap, "make_provider", recorded_provider)
    check = f'"{sys.executable}" -c "print(1)"'
    return runner.invoke(
        app,
        [
            "run",
            "tulis catatan",
            "--repo",
            str(repo),
            "--check",
            check,
            "--worker",
            "native",
            "--model",
            "recorded-model",
            "--auto-merge",
            "--plain",
        ],
    )


def test_native_run_completes_and_key_appears_nowhere(
    git_repo: Path,  # noqa: F811 - the imported fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay = Replay("tool_call", "text")
    result = _run_native(git_repo, monkeypatch, replay)

    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    # The model's write_file tool call went through the real tool and the real gate.
    assert git(git_repo, "show", "main:NOTES.md") == "catatan"
    status = get_run_status()
    assert status is not None and status.tasks[0].state == "DONE"
    assert status.tasks[0].summary == "Selesai: NOTES.md ditulis."

    # The key was sent to the API, and only there.
    assert len(replay.requests) == 2
    assert all(r.headers["authorization"] == f"Bearer {CANARY}" for r in replay.requests)
    assert all(body["model"] == "recorded-model" for body in replay.bodies())
    assert all(CANARY not in r.content.decode() for r in replay.requests)

    assert CANARY not in result.output  # type: ignore[attr-defined]
    assert _files_containing(harness_home(), CANARY.encode()) == []
    assert _files_containing(git_repo, CANARY.encode()) == []


def test_failed_auth_does_not_leak_the_key_either(
    git_repo: Path,  # noqa: F811 - the imported fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Worst case: the server echoes the key back inside its error message.
    result = _run_native(git_repo, monkeypatch, Replay("error_401", echo_key=True))

    assert result.exit_code == 1, result.output  # type: ignore[attr-defined]
    status = get_run_status()
    assert status is not None and status.tasks[0].state == "FAILED"
    assert "HTTP 401" in status.tasks[0].summary
    assert CANARY not in status.tasks[0].summary
    assert CANARY not in result.output  # type: ignore[attr-defined]
    assert _files_containing(harness_home(), CANARY.encode()) == []
