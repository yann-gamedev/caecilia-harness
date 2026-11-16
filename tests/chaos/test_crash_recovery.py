"""Kill the orchestrator process at every held point, then `caecilia resume` (backlog M1-13)."""

from __future__ import annotations

import sys
from contextlib import closing
from pathlib import Path

import pytest

from caecilia_harness.paths import db_path, worktrees_dir
from caecilia_harness.store.db import open_db
from caecilia_harness.store.events import SqliteEventStore
from caecilia_harness.store.projections import rebuild_projections
from tests.chaos.conftest import Chaos
from tests.integration.conftest import git

pytestmark = pytest.mark.chaos

HOLD_POINTS = ["READY", "LEASED", "RUNNING", "AWAITING_GATE", "INTEGRATING", "MERGING", "MERGED"]

# Same run as the CLI would create, but with a task that must not be re-run automatically.
NON_IDEMPOTENT_RUN = """
import asyncio, sys
from pathlib import Path
from caecilia_harness.bootstrap import prepare_run
from caecilia_harness.interfaces.prompter import TerminalPrompter

async def main():
    prepared = await prepare_run(
        "efek samping sekali saja", Path(sys.argv[1]), [sys.executable, "-c", "print(1)"],
        TerminalPrompter(), worker_kind="fake", auto_merge=True, idempotent=False,
    )
    try:
        await prepared.execute()
    finally:
        prepared.close()

asyncio.run(main())
"""


def _task_state_and_types() -> tuple[str, list[str], bool]:
    """Final task state, its event types, and whether a projection rebuild changes anything."""
    with closing(open_db(db_path())) as conn:
        store = SqliteEventStore(conn)
        [task] = store.list_tasks(None)
        types = [event.type for event in store.read_stream(f"task:{task.id}")]
        before = [tuple(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")]
        rebuild_projections(conn)
        after = [tuple(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")]
        return task.state.value, types, before == after


def _integration_leftovers(repo: Path) -> list[str]:
    branches = [b for b in git(repo, "branch", "--list", "caecilia/integrate/*").splitlines() if b]
    folder = worktrees_dir() / "_integrate"
    folders = [str(p) for p in folder.iterdir()] if folder.is_dir() else []
    return branches + folders


@pytest.mark.parametrize("hold_at", HOLD_POINTS)
async def test_kill_then_resume_completes_exactly_once(chaos: Chaos, hold_at: str) -> None:
    base = git(chaos.repo, "rev-parse", "main")
    await chaos.start_and_kill_at(hold_at)

    code, output = await chaos.resume()

    assert code == 0, output
    state, types, rebuild_identical = _task_state_and_types()
    assert state == "DONE", output
    assert rebuild_identical
    # Exactly one task commit on the target, directly on top of the original base.
    assert len(chaos.task_commits()) == 1
    assert git(chaos.repo, "rev-parse", "main~1") == base
    assert git(chaos.repo, "show", "main:CAECILIA_FAKE.md") == "tulis catatan chaos"
    assert _integration_leftovers(chaos.repo) == []
    assert ("task.lease_expired" in types) == (hold_at in ("LEASED", "RUNNING"))
    assert types.count("gate.passed") == 1


async def test_non_idempotent_task_killed_while_running_needs_human(chaos: Chaos) -> None:
    base = git(chaos.repo, "rev-parse", "main")
    argv = [sys.executable, "-c", NON_IDEMPOTENT_RUN, str(chaos.repo)]
    await chaos.start_and_kill_at("RUNNING", argv)

    code, output = await chaos.resume()

    assert code == 2, output
    state, types, rebuild_identical = _task_state_and_types()
    assert state == "NEEDS_HUMAN"
    assert rebuild_identical
    assert "task.lease_expired" in types
    assert chaos.spawns() and len(chaos.spawns()) == 1  # never spawned a second time
    assert git(chaos.repo, "rev-parse", "main") == base
