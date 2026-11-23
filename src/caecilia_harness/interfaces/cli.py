"""Command-line entry point: `caecilia`.

Only input parsing and output formatting live here; the application logic is in `bootstrap.py`
so a TUI/GUI can reuse it (ADR-0012). Messages for people are in Indonesian; command and option
names stay English.
"""

from __future__ import annotations

import asyncio
import contextlib
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
    get_graph,
    get_run_status,
    list_runs,
    plan_goal,
    prepare_run,
    prepare_run_from_plan,
    resume_run,
    revise_graph,
    task_draft_from_file,
)
from caecilia_harness.core.graph import GraphError
from caecilia_harness.core.models import AddTask, CancelTask, GraphOp, RetryTask, SetBlockers
from caecilia_harness.core.orchestrator import (
    GraphRevisionConflictError,
    GraphRevisionError,
    OrchestratorError,
)
from caecilia_harness.interfaces.doctor import run_all
from caecilia_harness.interfaces.prompter import TerminalPrompter
from caecilia_harness.interfaces.render import (
    format_event,
    format_graph,
    format_plan,
    format_runs,
    format_status,
)
from caecilia_harness.planning.plan import PlanError, load_plan
from caecilia_harness.planning.planner import PlannerError
from caecilia_harness.project_config import (
    CliValues,
    ConfigError,
    MissingOptionError,
    NotAGitRepoError,
    ProjectConfig,
    project_settings,
    repo_root,
    resolve_options,
)
from caecilia_harness.project_config import (
    parse_check as parse_check,  # re-exported: moved out of interfaces in CLI-01
)
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


def _fail(message: str) -> typer.Exit:
    typer.echo(f"Error: {keys.redact(message)}", err=True)
    return typer.Exit(code=1)


RepoOpt = Annotated[
    Path | None,
    typer.Option("--repo", help="Repo Git (default: repo dari folder kerja saat ini)."),
]


def _project(repo: Path | None) -> tuple[Path, ProjectConfig | None]:
    """The repo to work on and its `.caecilia.toml` (ADR-0015: the working folder decides)."""
    try:
        return project_settings(Path.cwd(), repo)
    except (NotAGitRepoError, ConfigError) as exc:
        raise _fail(str(exc)) from exc


def _runs_repo(repo: Path | None) -> Path | None:
    """Repo whose runs `status`/`resume` look at: `--repo`, else the working folder's repo.

    Outside any repo (and without --repo) every run is shown, as before.
    """
    try:
        return repo_root(repo if repo is not None else Path.cwd())
    except NotAGitRepoError as exc:
        if repo is not None:
            raise _fail(str(exc)) from exc
        return None


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
    repo: RepoOpt = None,
    check: Annotated[
        str | None,
        typer.Option(
            "--check", help='Perintah pengecekan gate, mis. "uv run pytest" (atau `check`).'
        ),
    ] = None,
    worker: Annotated[
        WorkerChoice | None,
        typer.Option("--worker", help="Worker yang dipakai (atau `worker`; default native)."),
    ] = None,
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
        int | None,
        typer.Option(
            "--provider-slots", min=1, help="Request model bersamaan per provider (default 2)."
        ),
    ] = None,
    exec_slots: Annotated[
        int | None,
        typer.Option(
            "--exec-slots", min=1, help="Perintah exec bersamaan di seluruh task (default 2)."
        ),
    ] = None,
) -> None:
    """Start a run from GOAL (one task) or --plan FILE (several tasks with dependencies).

    The repo is the one around the working folder unless --repo is given. Options not given here
    come from `.caecilia.toml` (`caecilia init`), then from the defaults.
    Exit code: 0 = DONE, 2 = NEEDS_HUMAN, 1 = otherwise. With --plan, --worker is the default
    for tasks that do not name a worker, and --target overrides the plan's target.
    """
    if (goal is None) == (plan is None):
        raise _fail("sebutkan tepat satu: GOAL atau --plan FILE")
    root, config = _project(repo)
    cli = CliValues(
        check=check,
        worker=worker.value if worker is not None else None,
        model=model,
        max_parallel=max_parallel,
        provider_slots=provider_slots,
        exec_slots=exec_slots,
        target=target,
    )
    try:
        options = resolve_options(cli, config)
        argv = options.require_check()
        if options.worker == "native" and not options.model:
            raise MissingOptionError("--model", "model", "wajib untuk worker native")
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    if plan is not None:
        start = prepare_run_from_plan(
            plan,
            root,
            argv,
            TerminalPrompter(),
            worker_kind=options.worker,
            target=options.target,
            auto_merge=auto_merge,
            unsafe_no_sandbox=unsafe_no_sandbox,
            model=options.model,
            max_parallel=options.max_parallel,
            provider_slots=options.provider_slots,
            exec_slots=options.exec_slots,
        )
    else:
        assert goal is not None  # noqa: S101 - checked above
        start = prepare_run(
            goal,
            root,
            argv,
            TerminalPrompter(),
            worker_kind=options.worker,
            target=options.target,
            auto_merge=auto_merge,
            unsafe_no_sandbox=unsafe_no_sandbox,
            model=options.model,
            max_parallel=options.max_parallel,
            provider_slots=options.provider_slots,
            exec_slots=options.exec_slots,
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
    repo: RepoOpt = None,
) -> None:
    """Recover after a crash and continue the latest unfinished run of this repo (or --run)."""
    start = resume_run(TerminalPrompter(), run_id, unsafe_no_sandbox, repo=_runs_repo(repo))
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
    repo: RepoOpt = None,
) -> None:
    """Show this repo's latest run (or --run) and its tasks. Read-only: works during a run."""
    try:
        current = get_run_status(run_id, repo=_runs_repo(repo))
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
    repo: RepoOpt = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Nama model di provider (atau `model`).")
    ] = None,
    out: Annotated[Path, typer.Option("--out", help="File plan yang ditulis.")] = Path("plan.toml"),
    max_tasks: Annotated[
        int, typer.Option("--max-tasks", min=1, help="Jumlah task paling banyak.")
    ] = 8,
    force: Annotated[bool, typer.Option("--force", help="Timpa file yang sudah ada.")] = False,
    plain: PlainOpt = False,
) -> None:
    """Let the model split GOAL into a plan.toml. Nothing is run: review the file first."""
    root, config = _project(repo)
    try:
        options = resolve_options(CliValues(model=model), config)
        if not options.model:
            raise MissingOptionError("--model", "model")
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    try:
        result = asyncio.run(
            plan_goal(goal, root, options.model, out, max_tasks=max_tasks, force=force)
        )
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
    check_hint = "" if options.check else ' --check "<perintah cek>"'
    typer.echo(f"  caecilia run --plan {out}{check_hint}")


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


graph_app = typer.Typer(
    no_args_is_help=True,
    help="Lihat dan ubah graf task sebuah run yang sedang berhenti (lalu `caecilia resume`).",
)
app.add_typer(graph_app, name="graph")

RunOpt = Annotated[str, typer.Option("--run", help="Id run.")]
ExpectOpt = Annotated[
    int | None,
    typer.Option("--expect-revision", help="Revisi graf yang diharapkan (untuk skrip)."),
]


def _ids(text: str | None) -> list[str]:
    """`a, b` -> ["a", "b"]; empty means no blockers."""
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def _revise(run_id: str, ops: list[GraphOp], expect: int | None) -> None:
    """Write one revision and report it; every failure ends with a clear message and exit 1."""
    try:
        revision = revise_graph(run_id, ops, expect)
    except WriterLockedError as exc:
        raise _fail(
            "run sedang dieksekusi oleh proses lain (writer lock dipegang); ubah graf setelah "
            "run itu berhenti"
        ) from exc
    except GraphRevisionConflictError as exc:
        raise _fail(f"{exc} (lihat `caecilia graph show --run {run_id}`)") from exc
    except GraphRevisionError as exc:
        typer.echo("Revisi graf ditolak; tidak ada yang diubah:", err=True)
        for problem in exc.problems:
            typer.echo(f"  - {problem}", err=True)
        raise typer.Exit(code=1) from exc
    except _USER_ERRORS as exc:
        raise _fail(str(exc)) from exc
    typer.echo(
        f"Revisi graf sekarang: {revision}. Lanjutkan dengan `caecilia resume --run {run_id}`."
    )


@graph_app.command("show")
def graph_show(
    run_id: Annotated[
        str | None, typer.Option("--run", help="Id run (default: run terakhir).")
    ] = None,
    plain: PlainOpt = False,
) -> None:
    """Show the graph in dependency order with its revision. Read-only: works during a run."""
    try:
        view = get_graph(run_id)
    except _USER_ERRORS as exc:
        raise _fail(str(exc)) from exc
    for line in format_graph(view, plain):
        typer.echo(line)


@graph_app.command("add")
def graph_add(
    run_id: RunOpt,
    file: Annotated[Path, typer.Option("--file", help="File berisi satu [[task]].")],
    blocked_by: Annotated[
        str | None, typer.Option("--blocked-by", help="Blocker tambahan: a,b (id plan).")
    ] = None,
    expect: ExpectOpt = None,
) -> None:
    """Add a task to the run."""
    try:
        draft = task_draft_from_file(file)
    except PlanError as exc:
        raise _fail(str(exc)) from exc
    _revise(run_id, [AddTask(task=draft, blocked_by=_ids(blocked_by))], expect)


@graph_app.command("deps")
def graph_deps(
    run_id: RunOpt,
    task: Annotated[str, typer.Argument(help="Id plan task (yang masih PENDING).")],
    blocked_by: Annotated[
        str, typer.Option("--blocked-by", help='Blocker baru: a,b; "" = tanpa blocker.')
    ],
    expect: ExpectOpt = None,
) -> None:
    """Replace the blockers of a task that has not started."""
    _revise(run_id, [SetBlockers(task_id=task, blocked_by=_ids(blocked_by))], expect)


@graph_app.command("cancel")
def graph_cancel(
    run_id: RunOpt,
    task: Annotated[str, typer.Argument(help="Id plan task.")],
    expect: ExpectOpt = None,
) -> None:
    """Cancel a task that is not finished and not being worked on or merged."""
    _revise(run_id, [CancelTask(task_id=task)], expect)


@graph_app.command("retry")
def graph_retry(
    run_id: RunOpt,
    task: Annotated[str, typer.Argument(help="Id plan task yang NEEDS_HUMAN.")],
    expect: ExpectOpt = None,
) -> None:
    """Send a NEEDS_HUMAN task back to READY after you fixed the problem."""
    _revise(run_id, [RetryTask(task_id=task)], expect)


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
