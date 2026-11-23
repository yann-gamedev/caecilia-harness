"""Benchmark tooling end to end with the fake worker: no network, no API key (backlog M2-08)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bench.run_bench import RunConfig, _cost, is_allowed, main, run_many
from bench.workloads import W1

pytestmark = pytest.mark.bench

FIELDS = {
    "run_id",
    "status",
    "wall_time_s",
    "task_durations_s",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "gate_failed",
    "rework",
    "needs_human",
    "tasks",
    "tasks_done",
    "timestamp",
    "harness_version",
    "harness_commit",
    "workload",
    "mode",
    "max_parallel",
    "worker",
    "model",
    "run",
    "check_passed_after",
    "commands_allowed",
    "commands_refused",
}


async def test_w1_serial_and_parallel_with_the_fake_worker(tmp_path: Path) -> None:
    out = tmp_path / "results.jsonl"
    # 1 s of fake work per task (more than the prompt's 0.3 s): the gate is serial in both
    # modes, so a longer worker phase keeps "parallel is faster" well above timing noise.
    common = {"workload": W1, "worker": "fake", "fake_delay": 1.0, "max_parallel": 4}
    serial = await run_many(RunConfig(mode="serial", **common), 1, out, tmp_path)  # type: ignore[arg-type]
    parallel = await run_many(RunConfig(mode="parallel", **common), 1, out, tmp_path)  # type: ignore[arg-type]

    [s], [p] = serial, parallel
    assert s["status"] == p["status"] == "done"
    assert (s["max_parallel"], p["max_parallel"]) == (1, 4)
    assert p["wall_time_s"] < s["wall_time_s"]
    assert s["tasks_done"] == p["tasks_done"] == 4
    assert s["check_passed_after"] and p["check_passed_after"]
    assert s["cost_usd"] is None  # no prices given

    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    for record in lines:
        assert set(record) >= FIELDS, FIELDS - set(record)
        assert set(record["task_durations_s"]) == {
            "api-health",
            "api-version",
            "web-status",
            "web-footer",
        }


def test_prices_give_a_cost(tmp_path: Path) -> None:
    config = RunConfig(
        workload=W1, mode="serial", max_parallel=1, worker="fake", price_in=2.0, price_out=8.0
    )
    assert _cost({"input_tokens": 1_000_000, "output_tokens": 500_000}, config) == 6.0


@pytest.mark.parametrize(
    ("program", "allowed"),
    [
        ("python", True),
        ("C:\\Python312\\python.exe", True),
        ("/usr/bin/python3.12", True),
        ("py", True),
        ("uv", True),
        ("pytest", True),
        ("rm", False),
        ("powershell", False),
        ("pythonista", False),
    ],
)
def test_allow_list(program: str, allowed: bool) -> None:
    assert is_allowed(program) is allowed


def test_cli_requires_a_model_for_the_native_worker(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--workload", "W1", "--mode", "serial", "--worker", "native"])
    assert "--model" in capsys.readouterr().err
