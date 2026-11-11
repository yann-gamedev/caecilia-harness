"""Command-line entry point: `caecilia`.

Only input parsing and output formatting live here; the application logic is in `bootstrap.py`
so a TUI/GUI can reuse it (ADR-0012). Messages for people are in Indonesian; command and option
names stay English.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import signal
from collections.abc import Awaitable, Iterator
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import typer

from caecilia_harness import __version__
from caecilia_harness.bootstrap import (
    AppError,
    PreparedRun,
    ProviderNotConfiguredError,
    get_run_status,
    list_runs,
    prepare_run,
    resume_run,
)
from caecilia_harness.core.orchestrator import OrchestratorError
from caecilia_harness.interfaces.doctor import run_all
from caecilia_harness.interfaces.prompter import TerminalPrompter
from caecilia_harness.interfaces.render import format_event, format_runs, format_status
from caecilia_harness.store.lock import WriterLockedError

app = typer.Typer(
    name="caecilia",
    help="Caecilia Harness: AI orchestration harness.",
    no_args_is_help=True,
    add_completion=False,
)

_MARK = {"ok": "[ok]  ", "warn": "[warn]", "fail": "[FAIL]"}
_POLL_S = 0.2
_EXIT = {"done": 0, "needs_human": 2}
_USER_ERRORS = (AppError, ProviderNotConfiguredError, WriterLockedError, OrchestratorError)


class WorkerChoice(StrEnum):
    fake = "fake"
    native = "native"


PlainOpt = Annotated[
    bool, typer.Option("--plain", help="Tanpa warna dan tanpa karakter box drawing.")
]


def exit_code_for(status: str) -> int:
    """0 = DONE, 2 = NEEDS_HUMAN, 1 = FAILED / CANCELLED / anything else."""
    return _EXIT.get(status, 1)


def parse_check(cmd: str, posix: bool | None = None) -> list[str]:
    """Split a --check command into argv.

    Windows uses non-POSIX rules so backslashes in paths survive; quotes wrapping a whole token
    are then removed, since non-POSIX mode keeps them.
    """
    posix = os.name != "nt" if posix is None else posix
    tokens = shlex.split(cmd, posix=posix)
    if not posix:
        tokens = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in tokens]
    return tokens


def _fail(message: str) -> typer.Exit:
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(code=1)


@contextlib.contextmanager
def _ctrl_c(prepared: PreparedRun) -> Iterator[None]:
    """First Ctrl+C cancels workers softly, the second one hard (Windows: plain SIGINT)."""
    loop = asyncio.get_running_loop()
    presses = 0
    cancelling: set[asyncio.Task[None]] = set()  # keep references until the tasks finish

    def on_sigint() -> None:
        nonlocal presses
        presses += 1
        mode: Literal["soft", "hard"] = "soft" if presses == 1 else "hard"
        if mode == "soft":
            typer.echo("\nMembatalkan dengan halus... tekan Ctrl+C sekali lagi untuk paksa.")
        else:
            typer.echo("\nMembatalkan paksa.")
        task = loop.create_task(prepared.orchestrator.cancel_all(mode))
        cancelling.add(task)
        task.add_done_callback(cancelling.discard)

    try:
        previous = signal.signal(
            signal.SIGINT, lambda signum, frame: loop.call_soon_threadsafe(on_sigint)
        )
    except ValueError:  # not in the main thread: leave Ctrl+C alone
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


async def _drive(start: Awaitable[PreparedRun], plain: bool) -> str:
    prepared = await start
    try:
        typer.echo(f"Run {prepared.run_id} berjalan.")

        def flush() -> None:
            for event in prepared.new_events():
                typer.echo(format_event(event, plain))

        async def printer() -> None:
            while True:
                flush()
                await asyncio.sleep(_POLL_S)

        printing = asyncio.create_task(printer())
        try:
            with _ctrl_c(prepared):
                status = await prepared.execute()
        finally:
            printing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await printing
            flush()
        typer.echo(f"Run {prepared.run_id} selesai: {status}")
        return status
    finally:
        prepared.close()


@app.command()
def version() -> None:
    """Print the harness version."""
    typer.echo(__version__)


@app.command()
def doctor() -> None:
    """Check Python, Git, SQLite, sandbox support, and the data directory."""
    checks = run_all()
    for check in checks:
        typer.echo(f"{_MARK[check.level]} {check.name:<8} {check.detail}")
    if any(check.level == "fail" for check in checks):
        raise typer.Exit(code=1)


@app.command()
def run(
    goal: Annotated[str, typer.Argument(help="Apa yang harus dikerjakan.")],
    repo: Annotated[Path, typer.Option("--repo", help="Repo Git yang dikerjakan.")],
    check: Annotated[
        str, typer.Option("--check", help='Perintah pengecekan gate, mis. "uv run pytest".')
    ],
    worker: Annotated[
        WorkerChoice, typer.Option("--worker", help="Worker yang dipakai.")
    ] = WorkerChoice.native,
    target: Annotated[
        str | None,
        typer.Option("--target", help="Branch target (default: branch aktif di repo)."),
    ] = None,
    auto_merge: Annotated[
        bool, typer.Option("--auto-merge", help="Merge tanpa konfirmasi kalau gate lulus.")
    ] = False,
    unsafe_no_sandbox: Annotated[
        bool,
        typer.Option("--unsafe-no-sandbox", help="Izinkan exec tanpa sandbox (ADR-0008)."),
    ] = False,
    plain: PlainOpt = False,
) -> None:
    """Start a run with one task. Exit code: 0 = DONE, 2 = NEEDS_HUMAN, 1 = otherwise."""
    argv = parse_check(check)
    if not argv:
        raise _fail("--check tidak boleh kosong")
    start = prepare_run(
        goal,
        repo,
        argv,
        TerminalPrompter(),
        worker_kind=worker.value,
        target=target,
        auto_merge=auto_merge,
        unsafe_no_sandbox=unsafe_no_sandbox,
    )
    try:
        status = asyncio.run(_drive(start, plain))
    except _USER_ERRORS as exc:
        raise _fail(str(exc)) from exc
    raise typer.Exit(code=exit_code_for(status))


@app.command()
def resume(
    run_id: Annotated[
        str | None, typer.Option("--run", help="Run yang dilanjutkan (default: terakhir).")
    ] = None,
    unsafe_no_sandbox: Annotated[
        bool, typer.Option("--unsafe-no-sandbox", help="Izinkan exec tanpa sandbox.")
    ] = False,
    plain: PlainOpt = False,
) -> None:
    """Recover after a crash and continue the latest unfinished run (or --run)."""
    start = resume_run(TerminalPrompter(), run_id, unsafe_no_sandbox)
    try:
        status = asyncio.run(_drive(start, plain))
    except _USER_ERRORS as exc:
        raise _fail(str(exc)) from exc
    raise typer.Exit(code=exit_code_for(status))


@app.command()
def status(
    run_id: Annotated[
        str | None, typer.Option("--run", help="Run yang ditampilkan (default: terakhir).")
    ] = None,
    plain: PlainOpt = False,
) -> None:
    """Show a run and its tasks. Read-only: works while another run is in progress."""
    try:
        current = get_run_status(run_id)
    except AppError as exc:
        raise _fail(str(exc)) from exc
    if current is None:
        typer.echo("Belum ada run.")
        return
    for line in format_status(current, plain):
        typer.echo(line)


@app.command()
def runs(
    limit: Annotated[int, typer.Option("--limit", help="Jumlah run yang ditampilkan.")] = 20,
    plain: PlainOpt = False,
) -> None:
    """List recent runs."""
    found = list_runs(limit)
    if not found:
        typer.echo("Belum ada run.")
        return
    for line in format_runs(found, plain):
        typer.echo(line)


if __name__ == "__main__":
    app()
