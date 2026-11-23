"""Wire concrete adapters into an `Orchestrator`, and the application functions on top of it.

The only module (besides tests) allowed to import every package. Interfaces (CLI now, TUI/GUI
later) call functions here instead of assembling components themselves (ADR-0012).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from caecilia_harness import artifacts
from caecilia_harness.core.models import Action, Event, Workspace
from caecilia_harness.core.orchestrator import Orchestrator
from caecilia_harness.core.ports import ModelProvider, UserPrompter, WorkerAdapter
from caecilia_harness.core.prompting import SerializedPrompter
from caecilia_harness.gate.simple import SimpleGate
from caecilia_harness.paths import db_path, harness_home
from caecilia_harness.planning.plan import (
    Plan,
    dump_plan,
    load_plan,
    parse_plan,
    plan_id,
    plan_to_specs,
    run_budget,
    single_task_plan,
)
from caecilia_harness.planning.planner import Planner, PlanResult
from caecilia_harness.planning.repo_summary import summarize_repo
from caecilia_harness.providers.deepseek import DeepSeekProvider
from caecilia_harness.providers.limited import LimitedProvider
from caecilia_harness.security.approval import ApprovalPolicy, Approver, ExecRequest
from caecilia_harness.security.keys import get_api_key
from caecilia_harness.store.db import open_db, open_readonly
from caecilia_harness.store.events import SqliteEventStore
from caecilia_harness.store.lock import WriterLock
from caecilia_harness.store.projections import project_runs_and_tasks
from caecilia_harness.workers.fake import FakeWorker
from caecilia_harness.workers.native.worker import NativeWorker
from caecilia_harness.workspace.exec_runner import ExecRunner
from caecilia_harness.workspace.git import GitError, run_git
from caecilia_harness.workspace.git_worktree import GitWorktreeBackend
from caecilia_harness.workspace.sandbox import detect_sandbox

WorkerKind = Literal["fake", "native"]
WORKER_KINDS: tuple[str, ...] = ("fake", "native")
#: Decision D2 (docs/decisions-open.md): the first provider for the native worker.
NATIVE_PROVIDER = "deepseek"


class AppError(Exception):
    """A user-facing problem with the request (bad repo, unknown run, ...)."""


def artifact_info(digest: str) -> tuple[str, int]:
    """Path and size of a stored artifact, for the `artifacts` table."""
    path = artifacts.artifact_path(digest)
    return str(path), path.stat().st_size


def exec_approver(orchestrator: Orchestrator) -> Approver:
    """Adapt the orchestrator's recorded approval flow to the exec runner's `Approver`."""

    async def approve(req: ExecRequest) -> bool:
        action = Action(
            kind="exec",
            task_id=req.task_id or None,
            detail={"argv": list(req.argv), "cwd": str(req.cwd)},
        )
        return await orchestrator.approve(action)

    return approve


def _fake_spawn_log() -> Path | None:
    # Chaos suite only: lets tests count worker starts across killed processes.
    value = os.environ.get("CAECILIA_FAKE_SPAWN_LOG")
    return Path(value) if value else None


def make_provider(api_key: str) -> ModelProvider:
    """Build the model provider for the native worker (decision D2: DeepSeek).

    A separate function so tests can substitute a provider with a recorded transport.
    """
    return DeepSeekProvider(api_key)


def _native_worker(
    model: str | None,
    unsafe_no_sandbox: bool,
    orchestrator_ref: list[Orchestrator],
    provider_slots: asyncio.Semaphore,
    exec_slots: asyncio.Semaphore,
) -> NativeWorker:
    if not model:
        raise AppError("--model wajib diisi untuk --worker native (nama model di provider)")
    provider = LimitedProvider(make_provider(get_api_key(NATIVE_PROVIDER)), provider_slots)
    sandbox = detect_sandbox()

    async def approve(req: ExecRequest) -> bool:
        # Late-bound: the orchestrator is created after its workers.
        return await exec_approver(orchestrator_ref[0])(req)

    def runner_for(ws: Workspace) -> ExecRunner:
        return ExecRunner(
            Path(ws.path),
            ApprovalPolicy(),  # ADR-0008: every command is asked about by default
            approve,
            sandbox,
            unsafe_no_sandbox=unsafe_no_sandbox,
            task_id=ws.task_id,
            slots=exec_slots,
        )

    return NativeWorker(provider, model, runner_for)


def _assemble(
    repo_path: Path,
    worker_kinds: Collection[str],
    check_command: list[str],
    prompter: UserPrompter,
    auto_merge: bool,
    model: str | None = None,
    unsafe_no_sandbox: bool = False,
    provider_slots: int = 2,
    exec_slots: int = 2,
) -> tuple[Orchestrator, SqliteEventStore]:
    """Build every worker the run's tasks refer to, then lock, open the DB and wire it up."""
    for name, limit in (("provider_slots", provider_slots), ("exec_slots", exec_slots)):
        if type(limit) is not int or limit < 1:
            raise AppError(f"{name} wajib bilangan bulat minimal 1")
    unknown = sorted(set(worker_kinds) - set(WORKER_KINDS))
    if unknown:
        raise AppError(
            f"worker tidak dikenal: {', '.join(unknown)} (tersedia: {', '.join(WORKER_KINDS)})"
        )
    orchestrator_ref: list[Orchestrator] = []
    workers: dict[str, WorkerAdapter] = {"fake": FakeWorker(spawn_log=_fake_spawn_log())}
    if "native" in worker_kinds:
        # Fails here, before any lock or DB is touched, if the key or model is missing.
        workers["native"] = _native_worker(
            model,
            unsafe_no_sandbox,
            orchestrator_ref,
            asyncio.Semaphore(provider_slots),
            asyncio.Semaphore(exec_slots),
        )
    lock = WriterLock(harness_home() / "harness.lock")
    lock.acquire()
    try:
        conn = open_db(db_path())
    except BaseException:
        lock.release()
        raise
    try:
        store = SqliteEventStore(conn, projectors=[project_runs_and_tasks])
        # Parallel tasks may ask at the same time: questions go to the person one by one.
        questions = SerializedPrompter(prompter)
        gate = SimpleGate(
            repo_path, check_command, confirm=questions.confirm_merge, auto_merge=auto_merge
        )
        orchestrator = Orchestrator(
            store,
            GitWorktreeBackend(repo_path),
            gate,
            workers,
            questions,
            artifact_info=artifact_info,
            closers=[conn.close, lock.release],
        )
    except BaseException:
        conn.close()
        lock.release()
        raise
    orchestrator_ref.append(orchestrator)
    return orchestrator, store


def build_orchestrator(
    repo_path: Path,
    worker_kind: WorkerKind,
    check_command: list[str],
    prompter: UserPrompter,
    auto_merge: bool = False,
    unsafe_no_sandbox: bool = False,
    model: str | None = None,
    provider_slots: int = 2,
    exec_slots: int = 2,
) -> Orchestrator:
    """Assemble store, workspace backend, gate and worker. Call `close()` on the result.

    Takes the single-writer lock first: a second orchestrator fails with `WriterLockedError`.
    `worker_kind="native"` needs `model` and a stored API key (`MissingApiKeyError` otherwise);
    `unsafe_no_sandbox` only affects the native worker's exec runner.
    """
    return _assemble(
        repo_path,
        {worker_kind},
        check_command,
        prompter,
        auto_merge,
        model,
        unsafe_no_sandbox,
        provider_slots=provider_slots,
        exec_slots=exec_slots,
    )[0]


# --- application functions (shared by the CLI now and TUI/GUI later, ADR-0012) ---------------


@dataclass
class PreparedRun:
    """A run ready to execute in this process. Always `close()` it (releases the writer lock)."""

    orchestrator: Orchestrator
    run_id: str
    _store: SqliteEventStore
    _seen_seq: int = 0

    async def execute(self) -> str:
        """Drive the run; returns "done", "needs_human", "failed" or "cancelled"."""
        return await self.orchestrator.run(self.run_id)

    def new_events(self) -> list[Event]:
        """Events appended since the previous call, in global order (for live output)."""
        events = self._store.read_all(after_seq=self._seen_seq)
        if events:
            self._seen_seq = events[-1].seq
        return events

    def close(self) -> None:
        self.orchestrator.close()


@dataclass(frozen=True)
class RunSummary:
    id: str
    goal: str
    status: str
    created_at: datetime


@dataclass(frozen=True)
class TaskStatus:
    id: str
    state: str
    attempts: int
    worker: str
    summary: str
    plan_id: str = ""
    #: Blockers as plan ids (without the run prefix).
    blocked_by: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RunStatus:
    run: RunSummary
    tasks: list[TaskStatus] = field(default_factory=list)


def _check_dir(path: Path) -> None:
    if not path.is_dir():
        raise AppError(f"folder repo tidak ditemukan: {path}")


def _resolved(path: str) -> Path:
    return Path(path).resolve()


async def _repo_root(repo_path: Path) -> Path:
    _check_dir(repo_path)
    try:
        top = await run_git(["rev-parse", "--show-toplevel"], repo_path)
    except GitError as exc:
        raise AppError(f"bukan repo Git: {repo_path}") from exc
    return _resolved(top.text)


async def _target_and_base(repo: Path, target: str | None) -> tuple[str, str]:
    if target is None:
        head = await run_git(["symbolic-ref", "-q", "--short", "HEAD"], repo, check=False)
        if head.returncode != 0 or not head.text:
            raise AppError("HEAD repo sedang detached; sebutkan branch target dengan --target")
        target = head.text
    try:
        base = await run_git(["rev-parse", "--verify", f"refs/heads/{target}"], repo)
    except GitError as exc:
        raise AppError(f"branch target tidak ada: {target}") from exc
    return target, base.text


async def _prepare(
    plan: Plan,
    repo_path: Path,
    check_command: list[str],
    prompter: UserPrompter,
    worker_kind: WorkerKind,
    target: str | None,
    auto_merge: bool,
    unsafe_no_sandbox: bool,
    model: str | None,
    max_parallel: int | None,
    provider_slots: int,
    exec_slots: int,
) -> PreparedRun:
    if max_parallel is not None:
        if max_parallel < 1:
            raise AppError("max_parallel wajib minimal 1")
        plan = Plan.model_validate(plan.model_dump() | {"max_parallel": max_parallel})
    if not check_command:
        raise AppError("check command tidak boleh kosong")
    repo = await _repo_root(repo_path)
    # The CLI's --target wins over the plan's target.
    target, base = await _target_and_base(repo, target or plan.target)
    run_id = uuid.uuid4().hex[:10]
    specs = plan_to_specs(plan, run_id, default_worker=worker_kind)
    worker_kinds = sorted({spec.worker for spec in specs})
    orchestrator, store = _assemble(
        repo,
        worker_kinds,
        check_command,
        prompter,
        auto_merge,
        model,
        unsafe_no_sandbox,
        provider_slots=provider_slots,
        exec_slots=exec_slots,
    )
    try:
        await orchestrator.recover()
        options: dict[str, Any] = {
            "check_command": list(check_command),
            "worker_kind": worker_kind,
            "worker_kinds": worker_kinds,
            "auto_merge": auto_merge,
            "unsafe_no_sandbox": unsafe_no_sandbox,
            "model": model,
            "max_parallel": plan.max_parallel,
            "provider_slots": provider_slots,
            "exec_slots": exec_slots,
        }
        orchestrator.create_run(
            plan.goal,
            str(repo),
            base,
            specs,
            target,
            options,
            max_parallel=plan.max_parallel,
            budget=run_budget(plan),
        )
    except BaseException:
        orchestrator.close()
        raise
    return PreparedRun(orchestrator, run_id, store)


async def prepare_run(
    goal: str,
    repo_path: Path,
    check_command: list[str],
    prompter: UserPrompter,
    worker_kind: WorkerKind = "native",
    target: str | None = None,
    auto_merge: bool = False,
    unsafe_no_sandbox: bool = False,
    idempotent: bool = True,
    model: str | None = None,
    max_parallel: int | None = None,
    provider_slots: int = 2,
    exec_slots: int = 2,
) -> PreparedRun:
    """Create a run with one task for `goal` (a one-task plan, see `single_task_plan`).

    `idempotent=False` marks the task as unsafe to re-run automatically after a crash.
    Anything a crashed run left behind is recovered first.
    """
    if not goal.strip():
        raise AppError("goal tidak boleh kosong")
    plan = single_task_plan(goal, idempotent=idempotent)
    return await _prepare(
        plan,
        repo_path,
        check_command,
        prompter,
        worker_kind,
        target,
        auto_merge,
        unsafe_no_sandbox,
        model,
        max_parallel,
        provider_slots,
        exec_slots,
    )


async def prepare_run_from_plan(
    plan_path: Path,
    repo_path: Path,
    check_command: list[str],
    prompter: UserPrompter,
    worker_kind: WorkerKind = "native",
    target: str | None = None,
    auto_merge: bool = False,
    unsafe_no_sandbox: bool = False,
    model: str | None = None,
    max_parallel: int | None = None,
    provider_slots: int = 2,
    exec_slots: int = 2,
) -> PreparedRun:
    """Create a run from a `plan.toml` (`PlanError` lists every problem in it).

    `worker_kind` is the default for tasks that do not name a worker; `target` overrides the
    plan's target. Every worker the tasks refer to is built (e.g. fake and native together).
    """
    plan = load_plan(plan_path)
    return await _prepare(
        plan,
        repo_path,
        check_command,
        prompter,
        worker_kind,
        target,
        auto_merge,
        unsafe_no_sandbox,
        model,
        max_parallel,
        provider_slots,
        exec_slots,
    )


def _exists(path: Path) -> bool:
    return path.exists()


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


async def plan_goal(
    goal: str,
    repo_path: Path,
    model: str,
    out_path: Path,
    max_tasks: int = 8,
    force: bool = False,
) -> PlanResult:
    """Ask the model to plan `goal` and write the plan to `out_path` for the person to review.

    Never starts a run, never takes the writer lock and never touches the database (ADR-0013).
    The provider is built like the native worker's (decision D2, API key from `security.keys`).
    """
    if not goal.strip():
        raise AppError("goal tidak boleh kosong")
    if not model:
        raise AppError("--model wajib diisi (nama model di provider)")
    # Checked before calling the model, so a refusal costs nothing.
    if _exists(out_path) and not force:
        raise AppError(f"file sudah ada: {out_path} (pakai --force untuk menimpa)")
    repo = await _repo_root(repo_path)
    provider = LimitedProvider(make_provider(get_api_key(NATIVE_PROVIDER)), asyncio.Semaphore(1))
    summary = await summarize_repo(repo)
    result = await Planner(provider, model, max_tasks=max_tasks).propose(goal, summary)
    text = dump_plan(result.plan)
    parse_plan(text, str(out_path))  # what is written must load back (and pass `plan validate`)
    _write_text(out_path, text)
    return result


async def resume_run(
    prompter: UserPrompter, run_id: str | None = None, unsafe_no_sandbox: bool = False
) -> PreparedRun:
    """Rebuild the orchestrator of an unfinished run (the latest one by default) and recover."""
    if run_id is None:
        unfinished = [run for run in list_runs(limit=1000) if run.status == "running"]
        if not unfinished:
            raise AppError("tidak ada run yang belum selesai")
        run_id = unfinished[0].id
    created = _run_created(run_id)
    options = created.get("options") or {}
    orchestrator, store = _assemble(
        Path(created["repo_path"]),
        options.get("worker_kinds") or [options.get("worker_kind", "fake")],
        list(options.get("check_command") or []),
        prompter,
        bool(options.get("auto_merge", False)),
        options.get("model"),
        unsafe_no_sandbox or bool(options.get("unsafe_no_sandbox", False)),
        provider_slots=options.get("provider_slots", 2),
        exec_slots=options.get("exec_slots", 2),
    )
    try:
        await orchestrator.recover()
    except BaseException:
        orchestrator.close()
        raise
    return PreparedRun(orchestrator, run_id, store)


def _readonly_store() -> SqliteEventStore | None:
    """Readers never take the writer lock, so `caecilia status` works during a run."""
    if not db_path().exists():
        return None
    return SqliteEventStore(open_readonly(db_path()))


def _run_created(run_id: str) -> dict[str, Any]:
    store = _readonly_store()
    if store is not None:
        try:
            for event in store.read_stream(f"run:{run_id}"):
                if event.type == "run.created":
                    return event.payload
        finally:
            store.close()
    raise AppError(f"run tidak ditemukan: {run_id}")


def list_runs(limit: int = 20) -> list[RunSummary]:
    """Most recent runs first."""
    store = _readonly_store()
    if store is None:
        return []
    try:
        rows = store.connection.execute(
            "SELECT id, goal, status, created_at FROM runs "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        store.close()
    return [
        RunSummary(
            id=row["id"],
            goal=row["goal"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
        for row in rows
    ]


def get_run_status(run_id: str | None = None) -> RunStatus | None:
    """The run (latest by default) with its tasks; None if there are no runs yet."""
    runs = list_runs(limit=1000)
    if run_id is not None:
        runs = [run for run in runs if run.id == run_id]
        if not runs:
            raise AppError(f"run tidak ditemukan: {run_id}")
    if not runs:
        return None
    run = runs[0]
    store = _readonly_store()
    if store is None:  # pragma: no cover - list_runs just read it
        return RunStatus(run=run)
    try:
        tasks = [
            TaskStatus(
                id=task.id,
                state=task.state.value,
                attempts=task.attempts,
                worker=task.spec.worker,
                summary=_last_summary(store.read_stream(f"task:{task.id}")),
                plan_id=plan_id(task.id, run.id),
                blocked_by=[plan_id(blocker, run.id) for blocker in task.spec.blocked_by],
            )
            for task in store.list_tasks(run.id)
        ]
    finally:
        store.close()
    return RunStatus(run=run, tasks=tasks)


def _last_summary(events: list[Event]) -> str:
    """What happened last: the worker's own summary once DONE, else why the task is where it is."""
    changes = [event for event in events if event.type == "task.state_changed"]
    if not changes:
        return ""
    last = changes[-1].payload
    if last.get("to_state") == "DONE":
        for event in reversed(events):
            if event.type == "worker.result" and event.payload.get("summary"):
                return str(event.payload["summary"])
    return str(last.get("reason") or "")
