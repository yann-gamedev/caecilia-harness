"""Wire concrete adapters into an `Orchestrator`.

The only module (besides tests) allowed to import every package. Interfaces (CLI now, TUI/GUI
later) call functions here instead of assembling components themselves (ADR-0012).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from caecilia_harness import artifacts
from caecilia_harness.core.models import Action
from caecilia_harness.core.orchestrator import Orchestrator
from caecilia_harness.core.ports import UserPrompter
from caecilia_harness.gate.simple import SimpleGate
from caecilia_harness.paths import db_path, harness_home
from caecilia_harness.security.approval import Approver, ExecRequest
from caecilia_harness.store.db import open_db
from caecilia_harness.store.events import SqliteEventStore
from caecilia_harness.store.lock import WriterLock
from caecilia_harness.store.projections import project_runs_and_tasks
from caecilia_harness.workers.fake import FakeWorker
from caecilia_harness.workspace.git_worktree import GitWorktreeBackend

WorkerKind = Literal["fake", "native"]


class ProviderNotConfiguredError(RuntimeError):
    """The native worker needs a model provider, which is not chosen yet."""


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
        return Orchestrator(
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
