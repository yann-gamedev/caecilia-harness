"""The interactive `caecilia` session with scripted input (backlog CLI-02)."""

from __future__ import annotations

import asyncio
import dataclasses
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import cast

import pytest

from caecilia_harness.bootstrap import list_runs
from caecilia_harness.interfaces.prompter import LineReader, TerminalPrompter
from caecilia_harness.interfaces.repl import PROMPT, Repl
from caecilia_harness.paths import harness_home
from caecilia_harness.project_config import CliValues, option_sources, resolve_options
from caecilia_harness.session import Session
from caecilia_harness.store.lock import WriterLock
from tests.integration.conftest import git

CANARY = "sk-test-CANARY-repl"
CHECK = [sys.executable, "-c", "print(1)"]
Line = str | Callable[[], str]


class Script:
    """Stands in for `input`: hands out the lines in order, then end of input."""

    def __init__(self, *lines: Line) -> None:
        self.lines = list(lines)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.lines:
            raise EOFError
        line = self.lines.pop(0)
        return line() if callable(line) else line


def _no_editor(path: Path) -> None:
    raise AssertionError("the editor was not expected to open")


def _repl(
    repo: Path,
    *lines: Line,
    worker: str = "fake",
    model: str | None = None,
    editor: Callable[[Path], None] = _no_editor,
    history: Path | None = None,
    echo: Callable[[str], None] | None = None,
) -> tuple[Repl, list[str], Script]:
    out: list[str] = []
    script = Script(*lines)
    reader = LineReader(script)
    say = echo or out.append
    prompter = TerminalPrompter(reader=reader, isatty=lambda: True, echo=say)
    cli = CliValues(worker=worker, model=model)
    options = dataclasses.replace(resolve_options(cli, None), check=CHECK)
    session = Session(repo, options, prompter, option_sources(cli, None), auto_merge=True)
    repl = Repl(session, reader, say, editor=editor, plain=True, history=history)
    return repl, out, script


def _text(out: list[str]) -> str:
    return "\n".join(out)


async def test_goal_plan_yes_runs_to_done_and_returns_to_the_prompt(git_repo: Path) -> None:
    repl, out, script = _repl(git_repo, "tulis catatan", "y", "/status", "/exit")
    await repl.run()
    text = _text(out)
    assert "TASK" in text and "Plan: 1 task, 0 dependensi." in text
    assert "selesai: done" in text
    assert "DONE" in text.split("selesai: done", 1)[1]  # /status after the run
    assert git(git_repo, "show", "main:CAECILIA_FAKE.md") == "tulis catatan"
    assert script.prompts[0] == PROMPT
    assert script.prompts[1].startswith("Jalankan?")
    assert script.prompts[2:] == [PROMPT, PROMPT]


async def test_help_config_unknown_command_and_exit(git_repo: Path) -> None:
    repl, out, script = _repl(git_repo, "/help", "", "/config", "/bogus", "/exit", "never read")
    await repl.run()
    text = _text(out)
    for command in ("/plan <goal>", "/run [file]", "/resume [id]", "/save <file>", "/exit"):
        assert command in text
    assert "worker" in text and "fake  (cli)" in text
    assert "provider" in text and "(default)" in text
    assert "Command tidak dikenal: /bogus" in text
    assert script.lines == ["never read"]  # /exit ended the session


async def test_end_of_input_ends_the_session(git_repo: Path) -> None:
    repl, out, _ = _repl(git_repo)
    await repl.run()
    assert out[0].startswith("Caecilia Harness")


async def test_no_keeps_the_plan_then_save_and_run_the_file(git_repo: Path, tmp_path: Path) -> None:
    plan_file = tmp_path / "saved.toml"
    repl, out, _ = _repl(
        git_repo, "simpan dulu", "n", f"/save {plan_file}", f"/run {plan_file}", "/runs"
    )
    await repl.run()
    text = _text(out)
    assert "Plan tidak dijalankan" in text
    assert plan_file.is_file()
    assert "selesai: done" in text
    [run] = list_runs()
    assert run.goal == "simpan dulu" and run.id in text


async def test_plan_command_only_plans(git_repo: Path) -> None:
    repl, out, _ = _repl(git_repo, "/plan cuma rencana", "/plan", "/run")
    await repl.run()
    text = _text(out)
    assert "Pakai: /plan <goal>" in text
    assert "selesai: done" in text  # /run used the plan made by /plan
    assert [run.goal for run in list_runs()] == ["cuma rencana"]


async def test_edit_revalidates_the_plan_before_running(git_repo: Path) -> None:
    def rename(path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace('id = "main"', 'id = "diedit"'), encoding="utf-8")

    repl, out, _ = _repl(git_repo, "ubah aku", "e", "y", "/status", editor=rename)
    await repl.run()
    text = _text(out)
    assert text.count("Plan: 1 task") == 2  # shown again after the edit
    assert "selesai: done" in text
    assert "diedit" in text.split("selesai: done", 1)[1]


async def test_broken_edit_is_reported_and_nothing_runs(git_repo: Path) -> None:
    def break_it(path: Path) -> None:
        path.write_text("version = 1\ngoal = 'x'\n[[task]]\nid = 'a'\n", encoding="utf-8")

    repl, out, _ = _repl(git_repo, "rusakkan", "e", "/exit", editor=break_it)
    await repl.run()
    text = _text(out)
    assert "Plan tidak valid" in text
    assert "Plan tidak dijalankan" in text
    assert list_runs() == []


async def test_ctrl_c_during_a_run_stops_it_and_returns_to_the_prompt(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAECILIA_FAKE_DELAYS", "*=30")
    loop = asyncio.get_running_loop()
    out: list[str] = []

    def press_twice() -> None:
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, cast(FrameType, None))  # soft
        handler(signal.SIGINT, cast(FrameType, None))  # stop

    def echo(text: str) -> None:
        out.append(text)
        if text.endswith("berjalan."):
            loop.call_later(0.5, press_twice)

    repl, _, script = _repl(git_repo, "lama sekali", "y", "/status", "/exit", echo=echo)
    await asyncio.wait_for(repl.run(), timeout=60)
    text = _text(out)
    assert "Membatalkan paksa" in text and "dihentikan" in text
    assert script.prompts[-2:] == [PROMPT, PROMPT]  # back at the prompt for /status and /exit
    with WriterLock(harness_home() / "harness.lock"):  # the run was closed: lock is free
        pass
    [run] = list_runs()
    assert run.status == "running"  # stopped, can be resumed


async def test_ctrl_c_while_idle_drops_the_line_and_keeps_one_reader(git_repo: Path) -> None:
    release = threading.Event()

    def slow_exit() -> str:
        assert release.wait(10)
        return "/exit"

    repl, out, script = _repl(git_repo, slow_exit)
    loop = asyncio.get_running_loop()

    def press() -> None:
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, cast(FrameType, None))
        loop.call_later(0.2, release.set)

    loop.call_later(0.3, press)
    await asyncio.wait_for(repl.run(), timeout=20)
    assert "baris dibatalkan" in _text(out)
    assert len(script.prompts) == 1  # the interrupted read was reused, not doubled


async def test_banner_and_config_show_key_presence_never_the_key(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", CANARY)
    earlier, _, _ = _repl(git_repo)
    await earlier.session.propose("belum selesai")
    (await earlier.session.start()).close()  # an unfinished run to offer
    [unfinished] = list_runs()

    repl, out, _ = _repl(git_repo, "/config", worker="native", model="m1")
    await repl.run()
    text = _text(out)
    assert "deepseek ada" in text
    assert f"/resume {unfinished.id}" in text
    assert "m1  (cli)" in text
    assert CANARY not in text


async def test_history_keeps_commands_but_not_answers(git_repo: Path, tmp_path: Path) -> None:
    history = tmp_path / "home" / "history"
    repl, _, _ = _repl(git_repo, "goal satu", "n", "/runs", "/exit", history=history)
    await repl.run()
    assert history.read_text(encoding="utf-8").splitlines() == ["goal satu", "/runs", "/exit"]
