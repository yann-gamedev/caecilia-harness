"""CLI parsing, terminal prompter and event rendering (backlog M1-12)."""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import cast

import pytest
from typer.testing import CliRunner

from caecilia_harness.bootstrap import AppError, PreparedRun
from caecilia_harness.core.models import Action, Actor, Event, GateReport
from caecilia_harness.interfaces.cli import _ctrl_c, _drive, app, exit_code_for, parse_check
from caecilia_harness.interfaces.prompter import TerminalPrompter
from caecilia_harness.interfaces.render import format_event
from caecilia_harness.security.approval import ApprovalPolicy
from tests.conftest import MemoryKeyring
from tests.integration.conftest import git_repo  # noqa: F401 - fixture

runner = CliRunner()


@pytest.mark.parametrize("command", ["version", "doctor", "run", "resume", "status", "runs"])
def test_every_command_has_help(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output


def test_run_needs_a_repo_and_a_check_from_somewhere(git_repo: Path) -> None:  # noqa: F811
    # --repo and --check are optional (CLI-01), but some repo and some check must be found.
    outside = runner.invoke(app, ["run", "goal", "--check", "echo hi", "--worker", "fake"])
    assert outside.exit_code == 1 and "--repo" in outside.output
    no_check = runner.invoke(app, ["run", "goal", "--repo", str(git_repo), "--worker", "fake"])
    assert no_check.exit_code == 1 and "--check" in no_check.output


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


@pytest.mark.parametrize("limit", ["0", "-1"])
def test_run_rejects_invalid_max_parallel(limit: str) -> None:
    result = runner.invoke(
        app, ["run", "goal", "--repo", ".", "--check", "echo hi", "--max-parallel", limit]
    )
    assert result.exit_code == 2
    assert "--max-parallel" in result.output


@pytest.mark.parametrize("option", ["--provider-slots", "--exec-slots"])
@pytest.mark.parametrize("limit", ["0", "-1"])
def test_run_rejects_invalid_slot_limits(option: str, limit: str) -> None:
    result = runner.invoke(app, ["run", "goal", "--repo", ".", "--check", "echo hi", option, limit])
    assert result.exit_code == 2
    assert "Invalid value" in result.output
    assert option in result.output


@pytest.mark.parametrize("from_plan", [False, True])
@pytest.mark.parametrize("override", [False, True])
def test_run_passes_slot_limits_to_application(
    monkeypatch: pytest.MonkeyPatch,
    git_repo: Path,  # noqa: F811 - the imported fixture
    from_plan: bool,
    override: bool,
) -> None:
    from caecilia_harness.interfaces import cli

    received: dict[str, object] = {}

    async def prepare(*args: object, **kwargs: object) -> PreparedRun:
        received.update(kwargs)
        raise AppError("test stopped before execution")

    monkeypatch.setattr(cli, "prepare_run_from_plan" if from_plan else "prepare_run", prepare)
    args = ["run", "--repo", str(git_repo), "--check", "echo hi", "--worker", "fake"]
    args.extend(["--plan", "plan.toml"] if from_plan else ["goal"])
    if override:
        args.extend(["--provider-slots", "3", "--exec-slots", "4"])
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert "test stopped" in result.output
    assert received["provider_slots"] == (3 if override else 2)
    assert received["exec_slots"] == (4 if override else 2)


async def test_ctrl_c_soft_then_hard_cancels_waiting_run() -> None:
    modes: list[str] = []

    class Orchestrator:
        async def cancel_all(self, mode: str) -> None:
            modes.append(mode)

    class Prepared:
        orchestrator = Orchestrator()

    async def waiting() -> str:
        await asyncio.Event().wait()
        return "done"

    running = asyncio.create_task(waiting())
    try:
        with _ctrl_c(cast(PreparedRun, Prepared()), running):
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, cast(FrameType, None))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert modes == ["soft"]
            assert not running.done()
            handler(signal.SIGINT, cast(FrameType, None))
            with pytest.raises(asyncio.CancelledError):
                await running
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("cancel_outer", [False, True])
async def test_drive_distinguishes_outer_and_run_cancellation(cancel_outer: bool) -> None:
    entered = asyncio.Event()
    closed = False
    run_task: asyncio.Task[str] | None = None

    class Prepared:
        run_id = "r1"

        async def execute(self) -> str:
            nonlocal run_task
            run_task = asyncio.current_task()
            entered.set()
            await asyncio.Event().wait()
            return "done"

        def new_events(self) -> list[Event]:
            return []

        def close(self) -> None:
            nonlocal closed
            closed = True

    async def start() -> PreparedRun:
        return cast(PreparedRun, Prepared())

    driving = asyncio.create_task(_drive(start(), plain=True))
    await entered.wait()
    if cancel_outer:
        driving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await driving
    else:
        assert run_task is not None
        run_task.cancel()
        assert await driving == "cancelled"
    assert closed


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


async def test_slow_answer_does_not_block_the_event_loop() -> None:
    def slow_read(prompt: str) -> str:
        time.sleep(0.5)  # a person thinking; blocks only the reader thread
        return "y"

    prompter = TerminalPrompter(read_line=slow_read, isatty=lambda: True, echo=lambda _: None)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticking = asyncio.create_task(ticker())
    try:
        assert await prompter.ask_approval(_action("make")) is True
    finally:
        ticking.cancel()
    assert ticks >= 10  # the loop kept running while the answer was pending


async def test_end_of_input_means_no() -> None:
    def closed(prompt: str) -> str:
        raise EOFError

    prompter = TerminalPrompter(read_line=closed, isatty=lambda: True, echo=lambda _: None)
    assert await prompter.ask_approval(_action("make")) is False
    assert await prompter.confirm_merge(GateReport(task_id="t1", passed=True)) is False


async def test_reader_errors_reach_the_caller() -> None:
    def broken(prompt: str) -> str:
        raise OSError("terminal gone")

    prompter = TerminalPrompter(read_line=broken, isatty=lambda: True, echo=lambda _: None)
    with pytest.raises(OSError, match="terminal gone"):
        await prompter.ask_approval(_action("make"))


def test_cancelled_prompt_does_not_hold_program_shutdown() -> None:
    # A blocked input reader must not keep asyncio.run() alive after Ctrl+C.
    script = """
import asyncio
import threading
from caecilia_harness.interfaces.prompter import TerminalPrompter

async def main():
    entered = threading.Event()
    def read(prompt):
        entered.set()
        threading.Event().wait()
        return "y"
    prompter = TerminalPrompter(read_line=read)
    question = asyncio.create_task(prompter._read("answer: "))
    while not entered.is_set():
        await asyncio.sleep(0.01)
    question.cancel()
    await asyncio.gather(question, return_exceptions=True)

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


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


@pytest.mark.parametrize("stream", ["task:run-17-api", "approval:run-17-api"])
def test_event_line_has_plan_task_id(stream: str) -> None:
    event = _event("usage.recorded", {"input_tokens": 1})
    event = event.model_copy(update={"stream": stream})
    line = format_event(event, plain=True, run_id="run-17")
    assert "[api]" in line
    assert stream in line


def test_run_event_has_no_task_label() -> None:
    event = _event("run.created", {"goal": "g"}).model_copy(update={"stream": "run:run-17"})
    assert "[-]" in format_event(event, plain=True, run_id="run-17")


def test_format_event_color() -> None:
    line = format_event(_event("gate.failed", {"report": {"output_tail": "boom"}}), plain=False)
    assert "\x1b[" in line


# --- key commands ---------------------------------------------------------------------------------

CANARY = "sk-test-CANARY"


def test_key_set_stores_in_keyring_without_echoing(memory_keyring: MemoryKeyring) -> None:
    result = runner.invoke(app, ["key", "set", "deepseek"], input=f"{CANARY}\n")
    assert result.exit_code == 0, result.output
    assert memory_keyring.passwords == {("caecilia-harness", "deepseek"): CANARY}
    assert CANARY not in result.output


def test_key_status_shows_presence_only(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = runner.invoke(app, ["key", "status"])
    assert missing.exit_code == 0
    assert "deepseek" in missing.output and "tidak ada" in missing.output

    monkeypatch.setenv("DEEPSEEK_API_KEY", CANARY)
    from_env = runner.invoke(app, ["key", "status"])
    assert "DEEPSEEK_API_KEY" in from_env.output
    assert CANARY not in from_env.output

    runner.invoke(app, ["key", "set", "deepseek"], input=f"{CANARY}\n")
    stored = runner.invoke(app, ["key", "status"])
    assert "keychain" in stored.output
    assert CANARY not in stored.output


def test_key_set_gemini(memory_keyring: MemoryKeyring) -> None:
    result = runner.invoke(app, ["key", "set", "gemini"], input=f"{CANARY}\n")
    assert result.exit_code == 0, result.output
    assert "gemini" in result.output
    assert memory_keyring.passwords == {("caecilia-harness", "gemini"): CANARY}
    assert CANARY not in result.output


def test_key_status_lists_each_provider_on_its_own_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", CANARY)
    result = runner.invoke(app, ["key", "status"])
    assert result.exit_code == 0
    lines = {line.split()[0]: line for line in result.output.splitlines() if line.strip()}
    assert "tidak ada" in lines["deepseek"]
    assert "GEMINI_API_KEY" in lines["gemini"]
    assert CANARY not in result.output


def test_key_set_rejects_unknown_provider_and_empty_key(memory_keyring: MemoryKeyring) -> None:
    unknown = runner.invoke(app, ["key", "set", "nope"], input="x\n")
    assert unknown.exit_code == 1 and "tidak dikenal" in unknown.output
    empty = runner.invoke(app, ["key", "set", "deepseek"], input="   \n")
    assert empty.exit_code == 1
    assert memory_keyring.passwords == {}
