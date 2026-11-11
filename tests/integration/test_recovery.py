"""Crash recovery per task state (backlog M1-11).

Each test fakes the database left behind by a crashed orchestrator (boot "deadboot") by appending
events directly through the store, then starts a fresh orchestrator: `recover()` + `resume()`.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from caecilia_harness.bootstrap import artifact_info, build_orchestrator
from caecilia_harness.core.models import (
    Action,
    Actor,
    DiffArtifact,
    GateReport,
    NewEvent,
    OutboxItem,
    TaskSpec,
    TaskStateChange,
)
from caecilia_harness.core.orchestrator import Orchestrator
from caecilia_harness.core.ports import BeforeMerge
from caecilia_harness.core.states import TaskState
from caecilia_harness.gate.simple import SimpleGate
from caecilia_harness.paths import db_path, worktrees_dir
from caecilia_harness.store.db import open_db
from caecilia_harness.store.events import SqliteEventStore
from caecilia_harness.store.leases import lease_acquired_event, lease_released_event
from caecilia_harness.store.lock import WriterLockedError
from caecilia_harness.store.projections import project_runs_and_tasks, rebuild_projections
from caecilia_harness.workers.fake import FakeWorker
from caecilia_harness.workspace.git_worktree import GitWorktreeBackend
from tests.integration.conftest import git

S = TaskState
PASS = [sys.executable, "-c", "print('ok')"]
OLD_HOLDER = "oldhost:999:deadboot"
CRASHED = Actor(kind="orchestrator", id="deadboot")


class Prompter:
    async def ask_approval(self, action: Action) -> bool:
        return True

    async def confirm_merge(self, report: GateReport) -> bool:
        return True


class CountingGate:
    """Real SimpleGate that counts how often `check` runs."""

    def __init__(self, repo: Path) -> None:
        self.inner = SimpleGate(repo, PASS, confirm=Prompter().confirm_merge, auto_merge=True)
        self.calls = 0

    async def check(
        self, diff: DiffArtifact, target_branch: str, before_merge: BeforeMerge | None = None
    ) -> GateReport:
        self.calls += 1
        return await self.inner.check(diff, target_branch, before_merge)

    async def branch_head(self, branch: str) -> str:
        return await self.inner.branch_head(branch)


class Crashed:
    """Builds the event history a crashed orchestrator would have left."""

    def __init__(self, store: SqliteEventStore, repo: Path, idempotent: bool = True) -> None:
        self.store = store
        self.repo = repo
        self.base = git(repo, "rev-parse", "main")
        spec = TaskSpec(
            id="t1",
            run_id="r1",
            title="notes",
            instructions="add notes",
            worker="fake",
            idempotent=idempotent,
        )
        run_payload = {
            "run_id": "r1",
            "goal": "add notes",
            "repo_path": str(repo),
            "base_commit": self.base,
            "target_branch": "main",
            "budget": {},
        }
        store.append(
            [
                self._ev("run:r1", "run.created", run_payload),
                self._ev("task:t1", "task.created", {"spec": spec.model_dump(mode="json")}),
            ]
        )

    def _ev(self, stream: str, type_: str, payload: dict[str, Any]) -> NewEvent:
        return NewEvent(stream=stream, type=type_, actor=CRASHED, payload=payload)

    def ev(self, type_: str, payload: dict[str, Any]) -> NewEvent:
        return self._ev("task:t1", type_, payload)

    def to(
        self,
        *states: TaskState,
        lease_expires_in: float = 3600,
        extra: dict[TaskState, Sequence[NewEvent]] | None = None,
    ) -> None:
        """Walk the task through `states`, with the lease events the old process wrote."""
        for target in states:
            task = self.store.get_task("t1")
            change = TaskStateChange(
                task_id="t1", from_state=task.state, to_state=target, revision=task.revision + 1
            )
            batch = [self.ev("task.state_changed", change.model_dump(mode="json"))]
            if target is S.LEASED:
                expires = datetime.now(UTC) + timedelta(seconds=lease_expires_in)
                batch.append(
                    lease_acquired_event("t1", OLD_HOLDER, task.attempts + 1, expires, CRASHED)
                )
            if task.state is S.RUNNING:
                batch.append(lease_released_event("t1", OLD_HOLDER, CRASHED))
            batch += (extra or {}).get(target, [])
            self.store.append(batch)

    async def worktree_with_change(self, workspace_id: str = "t1") -> DiffArtifact:
        backend = GitWorktreeBackend(self.repo)
        ws = await backend.create(workspace_id, self.base)
        (Path(ws.path) / "NOTES.md").write_bytes(b"notes\n")
        return await backend.capture_diff(ws)

    def started(self, workspace_id: str = "t1") -> NewEvent:
        return self.ev(
            "worker.started",
            {
                "worker": "fake",
                "attempt": 1,
                "workspace_id": workspace_id,
                "path": str(worktrees_dir() / workspace_id),
                "branch": f"caecilia/{workspace_id}",
                "base_commit": self.base,
            },
        )

    def result(self, diff: DiffArtifact) -> NewEvent:
        return self.ev(
            "worker.result",
            {"attempt": 1, "success": True, "diff": diff.model_dump(mode="json")},
        )

    async def until_integrating(self, merging: dict[str, str] | None = None) -> DiffArtifact:
        diff = await self.worktree_with_change()
        integrating: list[NewEvent] = [self.ev("gate.started", {"attempt": 1})]
        if merging is not None:
            integrating.append(self.ev("gate.merging", merging))
        self.to(
            S.READY,
            S.LEASED,
            S.RUNNING,
            S.AWAITING_GATE,
            S.INTEGRATING,
            extra={
                S.RUNNING: [self.started()],
                S.AWAITING_GATE: [self.result(diff)],
                S.INTEGRATING: integrating,
            },
        )
        return diff


@pytest.fixture
def store() -> Iterator[SqliteEventStore]:
    s = SqliteEventStore(open_db(db_path()), projectors=[project_runs_and_tasks])
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def spawn_log(tmp_path: Path) -> Path:
    return tmp_path / "spawn.log"


def _orchestrator(
    store: SqliteEventStore, repo: Path, spawn_log: Path
) -> tuple[Orchestrator, CountingGate]:
    gate = CountingGate(repo)
    worker = FakeWorker(files={"NOTES.md": "notes\n"}, spawn_log=spawn_log)
    orch = Orchestrator(
        store,
        GitWorktreeBackend(repo),
        gate,
        {"fake": worker},
        Prompter(),
        artifact_info=artifact_info,
    )
    return orch, gate


def _types(store: SqliteEventStore) -> list[str]:
    return [e.type for e in store.read_stream("task:t1")]


def _spawns(spawn_log: Path) -> list[str]:
    return spawn_log.read_text(encoding="utf-8").splitlines() if spawn_log.exists() else []


# --- LEASED / RUNNING ---------------------------------------------------------------------------


async def test_leased_by_other_boot_goes_back_to_ready(
    store: SqliteEventStore, git_repo: Path, spawn_log: Path
) -> None:
    crashed = Crashed(store, git_repo)
    crashed.to(S.READY, S.LEASED)
    old_spawn = OutboxItem(
        idempotency_key="t1:1:old:spawn_worker",
        kind="spawn_worker",
        payload={"task_id": "t1", "attempt": 1, "revision": 3},
    )
    store.append([], outbox=[old_spawn])

    orch, _ = _orchestrator(store, git_repo, spawn_log)
    await orch.recover()

    assert store.get_task("t1").state is S.READY
    assert "task.lease_expired" in _types(store)
    assert store.list_leases() == []
    status = store._conn.execute(
        "SELECT status FROM outbox WHERE idempotency_key = ?", (old_spawn.idempotency_key,)
    ).fetchone()[0]
    assert status == "cancelled"

    assert await orch.resume("r1") == "done"
    assert _spawns(spawn_log) == ["t1:1"]  # the old spawn item was never replayed


async def test_running_with_expired_lease_retries_and_keeps_old_worktree(
    store: SqliteEventStore, git_repo: Path, spawn_log: Path
) -> None:
    crashed = Crashed(store, git_repo)
    await crashed.worktree_with_change()  # what the crashed worker left on disk
    crashed.to(
        S.READY, S.LEASED, S.RUNNING, lease_expires_in=-5, extra={S.RUNNING: [crashed.started()]}
    )

    orch, _ = _orchestrator(store, git_repo, spawn_log)
    await orch.recover()

    task = store.get_task("t1")
    assert (task.state, task.attempts) == (S.READY, 1)
    states = [
        e.payload["to_state"]
        for e in store.read_stream("task:t1")
        if e.type == "task.state_changed"
    ]
    assert states[-2:] == ["RECOVERING", "READY"]
    [expired] = [e for e in store.read_stream("task:t1") if e.type == "task.lease_expired"]
    assert expired.payload["holder"] == OLD_HOLDER
    assert expired.payload["kept_workspace"] == str(worktrees_dir() / "t1")

    assert await orch.resume("r1") == "done"
    assert (worktrees_dir() / "t1" / "NOTES.md").exists()  # old worktree kept
    assert git(git_repo, "show", "main:NOTES.md") == "notes"
    assert store.get_task("t1").attempts == 2


async def test_running_non_idempotent_needs_human_without_respawn(
    store: SqliteEventStore, git_repo: Path, spawn_log: Path
) -> None:
    crashed = Crashed(store, git_repo, idempotent=False)
    crashed.to(S.READY, S.LEASED, S.RUNNING, extra={S.RUNNING: [crashed.started()]})

    orch, _ = _orchestrator(store, git_repo, spawn_log)
    await orch.recover()
    assert store.get_task("t1").state is S.NEEDS_HUMAN
    assert await orch.resume("r1") == "needs_human"
    assert _spawns(spawn_log) == []


async def test_own_live_lease_is_left_alone(
    store: SqliteEventStore, git_repo: Path, spawn_log: Path
) -> None:
    Crashed(store, git_repo).to(S.READY)
    orch, _ = _orchestrator(store, git_repo, spawn_log)
    await orch.advance("t1")  # READY -> LEASED under this orchestrator's own lease
    await orch.recover()
    assert store.get_task("t1").state is S.LEASED
    assert [lease.holder for lease in store.list_leases()] == [orch.holder]
    assert await orch.run("r1") == "done"


# --- AWAITING_GATE / INTEGRATING ------------------------------------------------------------------


async def test_awaiting_gate_stays_and_gate_runs_on_resume(
    store: SqliteEventStore, git_repo: Path, spawn_log: Path
) -> None:
    crashed = Crashed(store, git_repo)
    diff = await crashed.worktree_with_change()
    crashed.to(
        S.READY,
        S.LEASED,
        S.RUNNING,
        S.AWAITING_GATE,
        extra={S.RUNNING: [crashed.started()], S.AWAITING_GATE: [crashed.result(diff)]},
    )

    orch, gate = _orchestrator(store, git_repo, spawn_log)
    await orch.recover()
    assert store.get_task("t1").state is S.AWAITING_GATE
    assert await orch.resume("r1") == "done"
    assert gate.calls == 1
    assert _spawns(spawn_log) == []
    assert not (worktrees_dir() / "t1").exists()  # cleaned up from the recorded worker.started


async def test_integrating_without_merging_reruns_gate(
    store: SqliteEventStore, git_repo: Path, spawn_log: Path
) -> None:
    await Crashed(store, git_repo).until_integrating()
    orch, gate = _orchestrator(store, git_repo, spawn_log)
    await orch.recover()
    assert store.get_task("t1").state is S.INTEGRATING

    assert await orch.resume("r1") == "done"
    assert gate.calls == 1
    started = [e for e in store.read_stream("task:t1") if e.type == "gate.started"]
    assert len(started) == 2
    assert started[1].idempotency_key is not None and orch.boot_id in started[1].idempotency_key
    assert git(git_repo, "show", "main:NOTES.md") == "notes"


async def test_merging_already_landed_is_done_without_gate(
    store: SqliteEventStore, git_repo: Path, spawn_log: Path
) -> None:
    base = git(git_repo, "rev-parse", "main")
    crashed = Crashed(store, git_repo)
    diff = await crashed.worktree_with_change("t1-pre")
    # The crashed gate had fast-forwarded main to `new` right before dying.
    git(git_repo, "merge", "--ff-only", "-q", diff.head_commit)
    git(git_repo, "worktree", "remove", "--force", str(worktrees_dir() / "t1-pre"))
    await crashed.until_integrating(merging={"old": base, "new": diff.head_commit})

    orch, gate = _orchestrator(store, git_repo, spawn_log)
    await orch.recover()

    assert store.get_task("t1").state is S.DONE
    assert gate.calls == 0
    assert _types(store)[-2:] == ["gate.passed", "task.state_changed"]
    assert await orch.resume("r1") == "done"
    assert gate.calls == 0


async def test_merging_not_landed_reruns_gate(
    store: SqliteEventStore, git_repo: Path, spawn_log: Path
) -> None:
    base = git(git_repo, "rev-parse", "main")
    diff = await Crashed(store, git_repo).until_integrating(merging={"old": base, "new": "f" * 40})
    assert git(git_repo, "rev-parse", "main") == base  # target still at `old`

    orch, gate = _orchestrator(store, git_repo, spawn_log)
    await orch.recover()
    assert store.get_task("t1").state is S.INTEGRATING
    assert await orch.resume("r1") == "done"
    assert gate.calls == 1
    assert git(git_repo, "rev-parse", "main~1") == base
    assert diff.files_changed == ["NOTES.md"]


async def test_merging_with_target_elsewhere_needs_human(
    store: SqliteEventStore, git_repo: Path, spawn_log: Path
) -> None:
    base = git(git_repo, "rev-parse", "main")
    await Crashed(store, git_repo).until_integrating(merging={"old": base, "new": "f" * 40})
    (git_repo / "other.txt").write_bytes(b"someone else\n")
    git(git_repo, "add", "other.txt")
    git(git_repo, "commit", "-q", "-m", "unrelated")

    orch, gate = _orchestrator(store, git_repo, spawn_log)
    await orch.recover()
    assert store.get_task("t1").state is S.NEEDS_HUMAN
    assert gate.calls == 0


# --- states that are simply resumed -------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        (),
        (S.READY,),
        (S.READY, S.LEASED, S.RUNNING, S.RETRY_WAIT),
        (S.READY, S.LEASED, S.RUNNING, S.AWAITING_GATE, S.INTEGRATING, S.REWORK),
    ],
    ids=["PENDING", "READY", "RETRY_WAIT", "REWORK"],
)
async def test_waiting_states_are_resumed(
    store: SqliteEventStore, git_repo: Path, spawn_log: Path, path: tuple[TaskState, ...]
) -> None:
    Crashed(store, git_repo).to(*path)
    expected = path[-1] if path else S.PENDING
    orch, _ = _orchestrator(store, git_repo, spawn_log)
    await orch.recover()
    assert store.get_task("t1").state is expected
    assert await orch.resume("r1") == "done"
    assert git(git_repo, "show", "main:NOTES.md") == "notes"


async def test_rebuild_after_recovery_is_identical(
    store: SqliteEventStore, git_repo: Path, spawn_log: Path
) -> None:
    crashed = Crashed(store, git_repo)
    crashed.to(S.READY, S.LEASED, S.RUNNING, extra={S.RUNNING: [crashed.started()]})
    orch, _ = _orchestrator(store, git_repo, spawn_log)
    await orch.recover()

    conn = store._conn
    before = [tuple(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")]
    rebuild_projections(conn)
    assert [tuple(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")] == before


def test_second_orchestrator_is_refused(git_repo: Path) -> None:
    first = build_orchestrator(git_repo, "fake", PASS, Prompter())
    try:
        with pytest.raises(WriterLockedError):
            build_orchestrator(git_repo, "fake", PASS, Prompter())
    finally:
        first.close()
    build_orchestrator(git_repo, "fake", PASS, Prompter()).close()  # free again after close
