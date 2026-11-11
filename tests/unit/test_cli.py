"""CLI parsing, terminal prompter and event rendering (backlog M1-12)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from caecilia_harness.core.models import Action, Actor, Event, GateReport
from caecilia_harness.interfaces.cli import app, exit_code_for, parse_check
from caecilia_harness.interfaces.prompter import TerminalPrompter
from caecilia_harness.interfaces.render import format_event
from caecilia_harness.security.approval import ApprovalPolicy

runner = CliRunner()


@pytest.mark.parametrize("command", ["version", "doctor", "run", "resume", "status", "runs"])
def test_every_command_has_help(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output


@pytest.mark.parametrize(
    ("args", "missing"),
    [
        (["run", "goal", "--check", "echo hi"], "--repo"),
        (["run", "goal", "--repo", "."], "--check"),
    ],
)
def test_run_requires_repo_and_check(args: list[str], missing: str) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code != 0
    assert missing in result.output


@pytest.mark.parametrize(
    ("cmd", "posix", "expected"),
    [
        ('python -c "print(1)"', True, ["python", "-c", "print(1)"]),
        ('python -c "print(1)"', False, ["python", "-c", "print(1)"]),
        ("uv run pytest -q", True, ["uv", "run", "pytest", "-q"]),
        ("uv run pytest -q", False, ["uv", "run", "pytest", "-q"]),
        (r"C:\tools\check.exe --fast", False, [r"C:\tools\check.exe", "--fast"]),
        (r'"C:\Program Files\py\python.exe" -V', False, [r"C:\Program Files\py\python.exe", "-V"]),
        ("python -c 'print(2)'", False, ["python", "-c", "print(2)"]),
        ("/usr/bin/env python3 -c 'print(3)'", True, ["/usr/bin/env", "python3", "-c", "print(3)"]),
    ],
)
def test_parse_check(cmd: str, posix: bool, expected: list[str]) -> None:
    assert parse_check(cmd, posix=posix) == expected


def test_exit_codes() -> None:
    assert exit_code_for("done") == 0
    assert exit_code_for("needs_human") == 2
    assert exit_code_for("failed") == 1
    assert exit_code_for("cancelled") == 1


# --- TerminalPrompter ---------------------------------------------------------------------------


def _action(*argv: str) -> Action:
    return Action(kind="exec", task_id="t1", detail={"argv": list(argv), "cwd": "/wt"})


class Answers:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answers.pop(0)


async def test_non_tty_refuses_approval_and_merge() -> None:
    lines: list[str] = []
    prompter = TerminalPrompter(read_line=Answers(), isatty=lambda: False, echo=lines.append)
    assert await prompter.ask_approval(_action("make", "test")) is False
    assert await prompter.confirm_merge(GateReport(task_id="t1", passed=True)) is False
    assert any("bukan terminal interaktif" in line for line in lines)


async def test_always_remembers_prefix() -> None:
    policy = ApprovalPolicy()
    answers = Answers("a")
    prompter = TerminalPrompter(policy, read_line=answers, isatty=lambda: True, echo=lambda _: None)

    assert await prompter.ask_approval(_action("uv", "run", "pytest")) is True
    assert await prompter.ask_approval(_action("uv", "run", "pytest", "-q")) is True
    assert len(answers.prompts) == 1  # the second request was not asked
    assert ("uv", "run", "pytest") in policy.remembered


async def test_yes_no_and_retry_on_unknown_answer() -> None:
    answers = Answers("maybe", "y", "n")
    prompter = TerminalPrompter(read_line=answers, isatty=lambda: True, echo=lambda _: None)
    assert await prompter.ask_approval(_action("ls")) is True
    assert await prompter.ask_approval(_action("ls")) is False
    assert len(answers.prompts) == 3


async def test_confirm_merge_on_tty() -> None:
    lines: list[str] = []
    prompter = TerminalPrompter(read_line=Answers("y"), isatty=lambda: True, echo=lines.append)
    report = GateReport(task_id="t1", passed=True, checks={"command": True}, output_tail="ok\n")
    assert await prompter.confirm_merge(report) is True
    assert any("command: ok" in line for line in lines)


# --- rendering -----------------------------------------------------------------------------------


def _event(type_: str, payload: dict[str, object]) -> Event:
    return Event(
        stream="task:t1",
        type=type_,
        actor=Actor(kind="orchestrator", id="o"),
        payload=payload,
        event_id="e",
        seq=7,
        stream_seq=3,
        ts=datetime(2026, 11, 11, 13, 5, 9, tzinfo=UTC),
    )


def test_format_event_plain() -> None:
    change = _event(
        "task.state_changed", {"from_state": "READY", "to_state": "LEASED", "reason": "attempt 1"}
    )
    line = format_event(change, plain=True)
    assert "#7" in line and "task.state_changed" in line and "task:t1" in line
    assert "READY -> LEASED" in line
    assert "\x1b" not in line
    assert all(ord(ch) < 0x2500 or ord(ch) > 0x257F for ch in line)  # no box drawing


def test_format_event_color() -> None:
    line = format_event(_event("gate.failed", {"report": {"output_tail": "boom"}}), plain=False)
    assert "\x1b[" in line
