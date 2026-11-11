"""Wire concrete adapters into an `Orchestrator`, and the application functions on top of it.

The only module (besides tests) allowed to import every package. Interfaces (CLI now, TUI/GUI
later) call functions here instead of assembling components themselves (ADR-0012).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from caecilia_harness import artifacts
from caecilia_harness.core.models import Action, Event, TaskSpec
from caecilia_harness.core.orchestrator import Orchestrator
from caecilia_harness.core.ports import UserPrompter
from caecilia_harness.gate.simple import SimpleGate
from caecilia_harness.paths import db_path, harness_home
from caecilia_harness.security.approval import Approver, ExecRequest
from caecilia_harness.store.db import open_db, open_readonly
from caecilia_harness.store.events import SqliteEventStore
from caecilia_harness.store.lock import WriterLock
from caecilia_harness.store.projections import project_runs_and_tasks
from caecilia_harness.workers.fake import FakeWorker
from caecilia_harness.workspace.git import GitError, run_git
from caecilia_harness.workspace.git_worktree import GitWorktreeBackend

WorkerKind = Literal["fake", "native"]


class ProviderNotConfiguredError(RuntimeError):
    """The native worker needs a model provider, which is not chosen yet."""


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


def _assemble(
    repo_path: Path,
    worker_kind: WorkerKind,
    check_command: list[str],
    prompter: UserPrompter,
    auto_merge: bool,
) -> tuple[Orchestrator, SqliteEventStore]:
    if worker_kind == "native":
        raise ProviderNotConfiguredError(
            "the native worker needs a model provider, which has not been chosen yet "
            "(open decision D2 in docs/decisions-open.md); use --worker fake for now"
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
        gate = SimpleGate(
            repo_path, check_command, confirm=prompter.confirm_merge, auto_merge=auto_merge
        )
        orchestrator = Orchestrator(
            store,
            GitWorktreeBackend(repo_path),
            gate,
            {"fake": FakeWorker()},
            prompter,
            artifact_info=artifact_info,
            closers=[conn.close, lock.release],
        )
    except BaseException:
        conn.close()
        lock.release()
        raise
    return orchestrator, store


def build_orchestrator(
    repo_path: Path,
    worker_kind: WorkerKind,
    check_command: list[str],
    prompter: UserPrompter,
    auto_merge: bool = False,
    unsafe_no_sandbox: bool = False,
) -> Orchestrator:
    """Assemble store, workspace backend, gate and worker. Call `close()` on the result.

    Takes the single-writer lock first: a second orchestrator fails with `WriterLockedError`.
    `unsafe_no_sandbox` only matters for the native worker's exec runner (prompt 14).
    """
    return _assemble(repo_path, worker_kind, check_command, prompter, auto_merge)[0]


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


async def prepare_run(
    goal: str,
    repo_path: Path,
    check_command: list[str],
    prompter: UserPrompter,
    worker_kind: WorkerKind = "native",
    target: str | None = None,
    auto_merge: bool = False,
    unsafe_no_sandbox: bool = False,
) -> PreparedRun:
    """Create a run with one task for `goal`.

    Base commit = HEAD of `target` (default: the branch checked out in the repo). Anything a
    crashed run left behind is recovered first.
    """
    if not goal.strip():
        raise AppError("goal tidak boleh kosong")
    if not check_command:
        raise AppError("check command tidak boleh kosong")
    repo = await _repo_root(repo_path)
    target, base = await _target_and_base(repo, target)
    orchestrator, store = _assemble(repo, worker_kind, check_command, prompter, auto_merge)
    try:
        await orchestrator.recover()
        run_id = uuid.uuid4().hex[:10]
        spec = TaskSpec(
            id=f"{run_id}-1",
            run_id=run_id,
            title=goal if len(goal) <= 80 else goal[:77] + "...",
            instructions=goal,
            worker=worker_kind,
        )
        options: dict[str, Any] = {
            "check_command": list(check_command),
            "worker_kind": worker_kind,
            "auto_merge": auto_merge,
            "unsafe_no_sandbox": unsafe_no_sandbox,
        }
        orchestrator.create_run(goal, str(repo), base, spec, target, options)
    except BaseException:
        orchestrator.close()
        raise
    return PreparedRun(orchestrator, run_id, store)


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
        options.get("worker_kind", "fake"),
        list(options.get("check_command") or []),
        prompter,
        bool(options.get("auto_merge", False)),
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
            )
            for task in store.list_tasks(run.id)
        ]
    finally:
        store.close()
    return RunStatus(run=run, tasks=tasks)


def _last_summary(events: list[Event]) -> str:
    for event in reversed(events):
        if event.type == "worker.result":
            return str(event.payload.get("summary") or event.payload.get("error") or "")
        if event.type == "task.state_changed" and event.payload.get("reason"):
            return str(event.payload["reason"])
    return ""
