"""What must hold after any crash + `caecilia resume`, whatever the scenario (backlog M2-07)."""

from __future__ import annotations

import re
import shutil
import subprocess
from contextlib import closing
from pathlib import Path
from typing import Any

from caecilia_harness.core.states import TaskState
from caecilia_harness.paths import worktrees_dir
from caecilia_harness.store.db import open_db
from caecilia_harness.store.events import SqliteEventStore
from caecilia_harness.store.projections import rebuild_projections
from tests.integration.conftest import git

_TABLES = ("runs", "tasks", "task_deps")


def _dump(store: SqliteEventStore) -> dict[str, list[tuple[Any, ...]]]:
    result: dict[str, list[tuple[Any, ...]]] = {}
    for table in _TABLES:
        query = f"SELECT * FROM {table} ORDER BY 1, 2"  # noqa: S608 - fixed table names
        result[table] = [tuple(row) for row in store.connection.execute(query)]
    return result


def _is_ancestor(repo: Path, commit: str, head: str) -> bool:
    exe = shutil.which("git")
    assert exe is not None
    done = subprocess.run(
        [exe, "merge-base", "--is-ancestor", commit, head], cwd=repo, capture_output=True
    )
    return done.returncode == 0


def _merged_commit(payload: dict[str, Any]) -> str | None:
    """The merge a `gate.passed` recorded, including one found by recovery."""
    report = payload.get("report") or {}
    return report.get("merged_commit") or payload.get("merged_commit")


def check_invariants(db_path: Path, repo_path: Path, target: str = "main") -> None:
    """Assert the database and the repo agree and nothing was left half done."""
    with closing(open_db(db_path)) as conn:
        store = SqliteEventStore(conn)

        before = _dump(store)
        rebuild_projections(conn)
        assert _dump(store) == before, "projections differ from a rebuild of the event log"

        tasks = store.list_tasks(None)
        busy = [t.id for t in tasks if t.state in (TaskState.LEASED, TaskState.RUNNING)]
        assert busy == [], f"tasks still LEASED/RUNNING: {busy}"
        assert store.list_leases() == [], "a lease is still held"
        assert store.pending_outbox() == [], "an outbox item is still pending"

        head = git(repo_path, "rev-parse", target)
        subjects = git(repo_path, "log", "--format=%s", target).splitlines()
        task_commits = [s for s in subjects if s.startswith("caecilia: task ")]
        changed_done = 0
        for task in tasks:
            if task.state is not TaskState.DONE:
                continue
            passed = [e for e in store.read_stream(f"task:{task.id}") if e.type == "gate.passed"]
            assert len(passed) == 1, f"{task.id}: {len(passed)} gate.passed events"
            merged = _merged_commit(passed[0].payload)
            if merged is None:
                continue  # nothing to integrate (empty diff)
            changed_done += 1
            assert _is_ancestor(repo_path, merged, head), f"{task.id}: {merged} is not in {target}"
            # Later attempts commit from a worktree named `<task id>-<attempt>`.
            own = re.compile(rf"caecilia: task {re.escape(task.id)}(-\d+)?$")
            mine = [s for s in task_commits if own.fullmatch(s)]
            assert len(mine) == 1, f"{task.id}: {len(mine)} commits on {target}: {mine}"
        assert len(task_commits) == changed_done, (
            f"{len(task_commits)} task commits on {target}, {changed_done} DONE tasks with changes"
        )

    leftovers = git(repo_path, "branch", "--list", "caecilia/integrate/*").split()
    assert leftovers == [], f"integration branches left: {leftovers}"
    integrate = worktrees_dir() / "_integrate"
    folders = [p.name for p in integrate.iterdir()] if integrate.is_dir() else []
    assert folders == [], f"integration worktrees left: {folders}"
