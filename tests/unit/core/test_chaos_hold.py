"""The chaos hold hook: which point, which task (backlog M2-07). Never active without the env."""

from __future__ import annotations

import pytest

from caecilia_harness.core import orchestrator
from caecilia_harness.core.orchestrator import HOLD_ENV


@pytest.fixture
def holds(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(orchestrator.time, "sleep", slept.append)
    return slept


@pytest.mark.parametrize(
    ("wanted", "point", "task_id", "expected"),
    [
        ("RUNNING", "RUNNING", "r1-a", True),  # any task
        ("RUNNING", "DONE", "r1-a", False),
        ("RUNNING@b", "RUNNING", "r1-b", True),
        ("RUNNING@b", "RUNNING", "r1-a", False),
        ("RUNNING@b", "RUNNING", "r1-ab", False),
        ("RUNNING@api-v2", "RUNNING", "abc123-api-v2", True),  # plan ids may contain '-'
        ("MERGED@a", "MERGED", None, False),
        ("BUDGET_EXCEEDED", "BUDGET_EXCEEDED", None, True),
    ],
)
def test_hold_matches_point_and_task(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    holds: list[float],
    wanted: str,
    point: str,
    task_id: str | None,
    expected: bool,
) -> None:
    monkeypatch.setenv(HOLD_ENV, wanted)
    orchestrator._chaos_hold(point, task_id)
    assert bool(holds) is expected
    assert (f"CAECILIA_HOLD {wanted}" in capsys.readouterr().out) is expected


def test_no_env_means_no_hold(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], holds: list[float]
) -> None:
    monkeypatch.delenv(HOLD_ENV, raising=False)
    orchestrator._chaos_hold("RUNNING", "r1-a")
    assert holds == [] and capsys.readouterr().out == ""
