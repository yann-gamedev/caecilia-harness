"""Kill the orchestrator while two tasks run in parallel, then resume (backlog M2-07)."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from caecilia_harness.bootstrap import list_runs
from caecilia_harness.paths import db_path
from caecilia_harness.store.db import open_readonly
from caecilia_harness.store.events import SqliteEventStore
from tests.chaos.conftest import Chaos, write_plan
from tests.chaos.invariants import check_invariants
from tests.integration.conftest import git

pytestmark = pytest.mark.chaos

INDEPENDENT = [("a", []), ("b", [])]
CHAIN = [("a", []), ("b", ["a"])]


def _snapshot() -> tuple[dict[str, str], dict[str, list[dict[str, Any]]]]:
    """Task states (by plan id) and each task's events, read without a lock."""
    with closing(SqliteEventStore(open_readonly(db_path()))) as store:
        tasks = store.list_tasks(None)
        states = {t.id.partition("-")[2]: t.state.value for t in tasks}
        events = {
            t.id.partition("-")[2]: [
                {"type": e.type, **e.payload} for e in store.read_stream(f"task:{t.id}")
            ]
            for t in tasks
        }
    return states, events


def _types(events: list[dict[str, Any]], type_: str) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] == type_]


def _merged(events: list[dict[str, Any]]) -> str:
    [passed] = _types(events, "gate.passed")
    return str((passed.get("report") or {}).get("merged_commit") or passed["merged_commit"])


async def _resume_and_check(chaos: Chaos, run_id: str | None = None) -> tuple[int, str]:
    code, output = await chaos.resume(run_id)
    check_invariants(db_path(), chaos.repo)
    return code, output


async def test_two_tasks_running_when_killed(chaos: Chaos, tmp_path: Path) -> None:
    chaos.extra_env["CAECILIA_FAKE_DELAYS"] = "*=2"
    await chaos.start_and_kill_at("RUNNING@b", chaos.plan_argv(write_plan(tmp_path, INDEPENDENT)))
    states, _ = _snapshot()
    assert states["b"] == "RUNNING"
    assert states["a"] in ("LEASED", "RUNNING")  # started alongside b

    code, output = await _resume_and_check(chaos)

    assert code == 0, output
    states, events = _snapshot()
    assert states == {"a": "DONE", "b": "DONE"}
    spawns = chaos.spawns()
    for task in ("a", "b"):
        assert sum(line.split(":")[0].endswith(f"-{task}") for line in spawns) <= 2
        assert len(_types(events[task], "task.lease_expired")) <= 1


async def test_killed_mid_merge_while_another_task_waits(chaos: Chaos, tmp_path: Path) -> None:
    chaos.extra_env["CAECILIA_FAKE_DELAYS"] = "*=0.3"
    await chaos.start_and_kill_at("MERGING@a", chaos.plan_argv(write_plan(tmp_path, INDEPENDENT)))
    states, _ = _snapshot()
    assert states["a"] == "INTEGRATING"

    code, output = await _resume_and_check(chaos)

    assert code == 0, output
    states, _ = _snapshot()
    assert states == {"a": "DONE", "b": "DONE"}
    assert git(chaos.repo, "rev-list", "--merges", "--count", "main") == "0"  # one straight line


async def test_landed_merge_is_not_redone_while_the_other_task_runs(
    chaos: Chaos, tmp_path: Path
) -> None:
    chaos.extra_env["CAECILIA_FAKE_DELAYS"] = "a=0.2,b=4"
    await chaos.start_and_kill_at("MERGED@a", chaos.plan_argv(write_plan(tmp_path, INDEPENDENT)))
    states, events = _snapshot()
    assert states["b"] == "RUNNING"
    merged_before = git(chaos.repo, "rev-parse", "main")

    code, output = await _resume_and_check(chaos)

    assert code == 0, output
    states, events = _snapshot()
    assert states == {"a": "DONE", "b": "DONE"}
    assert len(_types(events["a"], "gate.started")) == 1  # recovery saw the merge: no second gate
    assert _merged(events["a"]) == merged_before


async def test_chain_continues_from_the_landed_merge(chaos: Chaos, tmp_path: Path) -> None:
    chaos.extra_env["CAECILIA_FAKE_DELAYS"] = "*=0.2"
    await chaos.start_and_kill_at("MERGED@a", chaos.plan_argv(write_plan(tmp_path, CHAIN)))

    code, output = await _resume_and_check(chaos)

    assert code == 0, output
    states, events = _snapshot()
    assert states == {"a": "DONE", "b": "DONE"}
    [b_started] = _types(events["b"], "worker.started")
    assert b_started["base_commit"] == _merged(events["a"])


async def test_budget_stop_survives_a_crash(chaos: Chaos, tmp_path: Path) -> None:
    chaos.extra_env["CAECILIA_FAKE_USAGE"] = "10,5"
    plan = write_plan(
        tmp_path, [("a", []), ("b", []), ("c", [])], "[budget]\nmax_output_tokens = 3"
    )
    await chaos.start_and_kill_at("BUDGET_EXCEEDED", chaos.plan_argv(plan, max_parallel=1))
    _, events = _snapshot()
    leases_before = sum(len(_types(e, "task.lease_acquired")) for e in events.values())
    [run] = list_runs()

    code, output = await _resume_and_check(chaos, run.id)

    assert code == 2, output  # needs_human: the budget is spent
    states, events = _snapshot()
    assert sum(len(_types(e, "task.lease_acquired")) for e in events.values()) == leases_before
    assert "DONE" not in states.values()
    assert states["b"] == states["c"] == "READY"
    assert chaos.task_commits() == []
