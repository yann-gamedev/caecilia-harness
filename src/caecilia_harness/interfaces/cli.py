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
from typing import Annotated, Any

import typer
from typer.core import TyperGroup

from caecilia_harness import __version__
from caecilia_harness.bootstrap import (
    AppError,
    PreparedRun,
    get_run_status,
    list_runs,
    plan_goal,
    prepare_run,
    prepare_run_from_plan,
    resume_run,
)
from caecilia_harness.core.graph import GraphError
from caecilia_harness.core.orchestrator import OrchestratorError
from caecilia_harness.interfaces.doctor import run_all
from caecilia_harness.interfaces.prompter import TerminalPrompter
from caecilia_harness.interfaces.render import (
    format_event,
    format_plan,
    format_runs,
    format_status,
)
from caecilia_harness.planning.plan import PlanError, load_plan
from caecilia_harness.planning.planner import PlannerError
from caecilia_harness.security import keys
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
_USER_ERRORS = (
    AppError,
    keys.MissingApiKeyError,
    WriterLockedError,
    OrchestratorError,
    PlanError,
    GraphError,
)


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
    typer.echo(f"Error: {keys.redact(message)}", err=True)
    return typer.Exit(code=1)


@contextlib.contextmanager
def _ctrl_c(prepared: PreparedRun, running: asyncio.Task[str]) -> Iterator[None]:
    """First Ctrl+C cancels workers softly; the second stops the whole run.

    Stopping the run also works while a question is waiting for an answer: the scheduler then
    cancels every worker itself. Windows uses plain SIGINT.
    """
    loop = asyncio.get_running_loop()
    presses = 0
    cancelling: set[asyncio.Task[None]] = set()  # keep references until the tasks finish

    def on_sigint() -> None:
        nonlocal presses
        presses += 1
        if presses == 1:
            typer.echo("\nMembatalkan dengan halus... tekan Ctrl+C sekali lagi untuk paksa.")
            task = loop.create_task(prepared.orchestrator.cancel_all("soft"))
            cancelling.add(task)
            task.add_done_callback(cancelling.discard)
        else:
            typer.echo("\nMembatalkan paksa.")
            running.cancel()

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
                typer.echo(format_event(event, plain, run_id=prepared.run_id))

        async def printer() -> None:
            while True:
                flush()
                await asyncio.sleep(_POLL_S)

        printing = asyncio.create_task(printer())
        running = asyncio.create_task(prepared.execute())
        try:
            with _ctrl_c(prepared, running):
                try:
                    status = await running
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise  # caller cancellation must propagate, unlike second Ctrl+C
                    typer.echo(
                        f"Run {prepared.run_id} dihentikan. Lanjutkan dengan `caecilia resume`."
                    )
                    status = "cancelled"
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
    model: Annotated[
        str | None,
        typer.Option("--model", help="Nama model di provider (wajib untuk --worker native)."),
    ] = None,
    plan: Annotated[
        Path | None,
        typer.Option("--plan", help="File plan.toml dengan banyak task (ganti GOAL)."),
    ] = None,
    max_parallel: Annotated[
        int | None,
        typer.Option(
            "--max-parallel",
            min=1,
            help="Task yang boleh bekerja bersamaan (default: nilai di plan, atau 2).",
        ),
    ] = None,
    plain: PlainOpt = False,
    goal: Annotated[
        str | None, typer.Argument(help="Apa yang harus dikerjakan (atau pakai --plan).")
    ] = None,
    provider_slots: Annotated[
        int,
        typer.Option("--provider-slots", min=1, help="Request model bersamaan per provider."),
    ] = 2,
    exec_slots: Annotated[
        int,
        typer.Option("--exec-slots", min=1, help="Perintah exec bersamaan di seluruh task."),
    ] = 2,
) -> None:
    """Start a run from GOAL (one task) or --plan FILE (several tasks with dependencies).

    Exit code: 0 = DONE, 2 = NEEDS_HUMAN, 1 = otherwise. With --plan, --worker is the default
    for tasks that do not name a worker, and --target overrides the plan's target.
    """
    if (goal is None) == (plan is None):
        raise _fail("sebutkan tepat satu: GOAL atau --plan FILE")
    argv = parse_check(check)
    if not argv:
        raise _fail("--check tidak boleh kosong")
    if plan is not None:
        start = prepare_run_from_plan(
            plan,
            repo,
            argv,
            TerminalPrompter(),
            worker_kind=worker.value,
            target=target,
            auto_merge=auto_merge,
            unsafe_no_sandbox=unsafe_no_sandbox,
            model=model,
            max_parallel=max_parallel,
            provider_slots=provider_slots,
            exec_slots=exec_slots,
        )
    else:
        assert goal is not None  # noqa: S101 - checked above
        start = prepare_run(
            goal,
            repo,
            argv,
            TerminalPrompter(),
            worker_kind=worker.value,
            target=target,
            auto_merge=auto_merge,
            unsafe_no_sandbox=unsafe_no_sandbox,
            model=model,
            max_parallel=max_parallel,
            provider_slots=provider_slots,
            exec_slots=exec_slots,
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


class _PlanGroup(TyperGroup):
    """`caecilia plan GOAL ...` and `caecilia plan validate FILE` in one group.

    Click would read GOAL as a subcommand name, so anything that is not a known subcommand (or a
    help flag) is routed to `create`.
    """

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        if args and args[0] not in self.commands and args[0] not in ("--help", "-h"):
            args = ["create", *args]
        return super().parse_args(ctx, args)


plan_app = typer.Typer(
    cls=_PlanGroup,
    no_args_is_help=True,
    help="Buat plan.toml dengan model (`caecilia plan GOAL`), atau periksa plan yang ada.",
)
app.add_typer(plan_app, name="plan")


@plan_app.command("create")
def plan_create(
    goal: Annotated[str, typer.Argument(help="Tujuan yang dipecah menjadi task.")],
    repo: Annotated[Path, typer.Option("--repo", help="Repo Git yang direncanakan.")],
    model: Annotated[str, typer.Option("--model", help="Nama model di provider.")],
    out: Annotated[Path, typer.Option("--out", help="File plan yang ditulis.")] = Path("plan.toml"),
    max_tasks: Annotated[
        int, typer.Option("--max-tasks", min=1, help="Jumlah task paling banyak.")
    ] = 8,
    force: Annotated[bool, typer.Option("--force", help="Timpa file yang sudah ada.")] = False,
    plain: PlainOpt = False,
) -> None:
    """Let the model split GOAL into a plan.toml. Nothing is run: review the file first."""
    try:
        result = asyncio.run(plan_goal(goal, repo, model, out, max_tasks=max_tasks, force=force))
    except PlannerError as exc:
        typer.echo(f"Error: {keys.redact(str(exc))}", err=True)
        raise typer.Exit(code=1) from exc
    except _USER_ERRORS as exc:
        raise _fail(str(exc)) from exc
    for line in format_plan(result.plan, plain):
        typer.echo(line)
    usage = result.usage
    typer.echo("")
    typer.echo(
        f"Plan ditulis ke {out}: {len(result.plan.tasks)} task, {result.rounds} ronde, "
        f"token in {usage.input_tokens} / out {usage.output_tokens}."
    )
    typer.echo(f"Tinjau dan edit {out} lebih dulu, lalu jalankan:")
    typer.echo(f'  caecilia run --plan {out} --repo {repo} --check "<perintah cek>"')


@plan_app.command("validate")
def plan_validate(
    path: Annotated[Path, typer.Argument(help="File plan.toml.")],
) -> None:
    """Check a plan offline (no database, no lock) and print every problem found."""
    try:
        loaded = load_plan(path)
    except PlanError as exc:
        typer.echo(f"Plan tidak valid: {exc.source}", err=True)
        for problem in exc.problems:
            typer.echo(f"  - {problem}", err=True)
        raise typer.Exit(code=1) from exc
    edges = sum(len(task.blocked_by) for task in loaded.tasks)
    typer.echo(f"Plan valid: {len(loaded.tasks)} task, {edges} dependensi.")


key_app = typer.Typer(help="Kelola API key provider model (disimpan di keychain OS).")
app.add_typer(key_app, name="key")


@key_app.command("set")
def key_set(
    provider: Annotated[str, typer.Argument(help="Provider, mis. deepseek.")],
) -> None:
    """Store a provider API key in the OS keychain (input is hidden)."""
    try:
        keys.key_source(provider)  # validates the provider name before asking for the key
        secret = typer.prompt(f"API key untuk {provider}", hide_input=True)
        keys.set_api_key(provider, secret)
    except (keys.UnknownProviderError, ValueError) as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"API key {provider} disimpan di keychain OS.")


@key_app.command("status")
def key_status() -> None:
    """Show whether a key exists for each provider. Never prints the key itself."""
    labels = {"keyring": "ada (keychain OS)", "env": "ada (variabel env {env})", None: "tidak ada"}
    for provider, env_var in sorted(keys.ENV_VARS.items()):
        typer.echo(f"{provider:<10} {labels[keys.key_source(provider)].format(env=env_var)}")


if __name__ == "__main__":
    app()
