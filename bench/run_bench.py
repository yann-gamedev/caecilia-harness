"""Run a workload serially or in parallel and record one JSON line of metrics per run.

    python -m bench.run_bench --workload W1 --mode parallel --worker fake --runs 1

Every run gets a fresh CAECILIA_HOME and a fresh repo in a temporary folder, so runs never share
state. Metrics are read back from the run's own event log. See bench/README.md for real runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from bench.workloads import WORKLOADS, Workload, create_repo
from caecilia_harness import __version__
from caecilia_harness.bootstrap import WorkerKind, prepare_run_from_plan
from caecilia_harness.core.models import Action, Event, GateReport
from caecilia_harness.paths import db_path
from caecilia_harness.store.db import open_readonly
from caecilia_harness.store.events import SqliteEventStore

Mode = Literal["serial", "parallel"]

#: Commands the benchmark approves without asking: everything else is refused.
ALLOWED_PROGRAMS = ("python", "py", "uv", "pytest")
_PYTHON = re.compile(r"python(\d+(\.\d+)?)?")
_HARNESS_ROOT = Path(__file__).resolve().parents[1]


class BenchPrompter:
    """Unattended answers: allow-listed programs run, every other command is refused."""

    def __init__(self) -> None:
        self.allowed = 0
        self.refused: list[list[str]] = []

    async def ask_approval(self, action: Action) -> bool:
        argv = [str(arg) for arg in action.detail.get("argv", [])]
        if argv and is_allowed(argv[0]):
            self.allowed += 1
            return True
        self.refused.append(argv)
        return False

    async def confirm_merge(self, report: GateReport) -> bool:
        return True  # runs use auto-merge; never asked


def is_allowed(program: str) -> bool:
    """`python`, `py`, `uv`, `pytest` (any path, `.exe`, or `python3[.x]`)."""
    name = Path(program).name.lower().removesuffix(".exe")
    return name in ALLOWED_PROGRAMS or bool(_PYTHON.fullmatch(name))


@dataclass(frozen=True)
class RunConfig:
    workload: Workload
    mode: Mode
    max_parallel: int
    worker: WorkerKind
    model: str | None = None
    fake_delay: float = 2.0
    price_in: float | None = None
    price_out: float | None = None


@contextmanager
def _env(values: dict[str, str]) -> Iterator[None]:
    """Set environment variables for one run (the harness reads them at use time)."""
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _seconds(start: datetime, end: datetime) -> float:
    return round((end - start).total_seconds(), 3)


def metrics_from_events(events: Sequence[Event], run_id: str) -> dict[str, Any]:
    """Everything the report needs, from one run's event log."""
    by_type: dict[str, list[Event]] = {}
    for event in events:
        by_type.setdefault(event.type, []).append(event)
    [created] = by_type["run.created"]
    finished = by_type.get("run.finished", [])
    changes = by_type.get("task.state_changed", [])
    prefix = f"{run_id}-"

    task_durations: dict[str, float] = {}
    final_states: dict[str, str] = {}
    for event in changes:
        task = event.stream.removeprefix("task:").removeprefix(prefix)
        final_states[task] = str(event.payload["to_state"])
    for task in final_states:
        own = [e for e in changes if e.stream == f"task:{prefix}{task}"]
        leased = [e for e in own if e.payload["to_state"] == "LEASED"]
        if leased:
            task_durations[task] = _seconds(leased[0].ts, own[-1].ts)

    usage = by_type.get("usage.recorded", [])
    return {
        "run_id": run_id,
        "status": str(finished[-1].payload["status"]) if finished else "unfinished",
        "wall_time_s": _seconds(created.ts, finished[-1].ts) if finished else None,
        "task_durations_s": task_durations,
        "input_tokens": sum(int(e.payload.get("input_tokens", 0)) for e in usage),
        "output_tokens": sum(int(e.payload.get("output_tokens", 0)) for e in usage),
        "gate_failed": len(by_type.get("gate.failed", [])),
        "rework": sum(1 for e in changes if e.payload["to_state"] == "REWORK"),
        "needs_human": sum(1 for state in final_states.values() if state == "NEEDS_HUMAN"),
        "tasks": len(final_states),
        "tasks_done": sum(1 for state in final_states.values() if state == "DONE"),
    }


def _cost(record: dict[str, Any], config: RunConfig) -> float | None:
    if config.price_in is None or config.price_out is None:
        return None
    tokens_in, tokens_out = int(record["input_tokens"]), int(record["output_tokens"])
    return round((tokens_in * config.price_in + tokens_out * config.price_out) / 1_000_000, 6)


def _harness_commit() -> str | None:
    try:
        done = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - git from PATH, like the harness itself
            cwd=_HARNESS_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return done.stdout.strip()


def _check_passes(workload: Workload, repo: Path) -> bool:
    """Does the repo's own check pass on the target branch after the run?"""
    done = subprocess.run(workload.check, cwd=repo, capture_output=True)
    return done.returncode == 0


async def run_once(config: RunConfig, index: int, folder: Path) -> dict[str, Any]:
    """One isolated run: fresh home, fresh repo, then metrics from the event log."""
    home, repo = folder / f"home-{index}", folder / f"repo-{index}"
    create_repo(config.workload, repo)
    plan = folder / f"plan-{index}.toml"
    plan.write_text(config.workload.plan, encoding="utf-8")
    env = {"CAECILIA_HOME": str(home)}
    if config.worker == "fake":
        env["CAECILIA_FAKE_DELAYS"] = f"*={config.fake_delay}"
    max_parallel = 1 if config.mode == "serial" else config.max_parallel
    prompter = BenchPrompter()
    with _env(env):
        prepared = await prepare_run_from_plan(
            plan,
            repo,
            config.workload.check,
            prompter,
            worker_kind=config.worker,
            auto_merge=True,
            model=config.model,
            max_parallel=max_parallel,
        )
        try:
            await prepared.execute()
        finally:
            prepared.close()
        with closing(SqliteEventStore(open_readonly(db_path()))) as store:
            events = store.read_all()
    record = metrics_from_events(events, prepared.run_id)
    record |= {
        "timestamp": datetime.now(UTC).isoformat(),
        "harness_version": __version__,
        "harness_commit": _harness_commit(),
        "workload": config.workload.name,
        "mode": config.mode,
        "max_parallel": max_parallel,
        "worker": config.worker,
        "model": config.model,
        "run": index,
        "cost_usd": None,
        "check_passed_after": _check_passes(config.workload, repo),
        "commands_allowed": prompter.allowed,
        "commands_refused": len(prompter.refused),
    }
    record["cost_usd"] = _cost(record, config)
    return record


async def run_many(
    config: RunConfig, runs: int, out: Path, workdir: Path | None = None
) -> list[dict[str, Any]]:
    """`runs` isolated runs, each appended to `out` as one JSON line as soon as it ends."""
    records = []
    with tempfile.TemporaryDirectory(prefix="caecilia-bench-", dir=workdir) as tmp:
        for index in range(1, runs + 1):
            record = await run_once(config, index, Path(tmp))
            records.append(record)
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
            print(
                f"{config.workload.name} {config.mode} run {index}/{runs}: {record['status']}, "
                f"{record['wall_time_s']} s, tokens in {record['input_tokens']} / "
                f"out {record['output_tokens']}",
                flush=True,
            )
    return records


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--workload", choices=sorted(WORKLOADS), required=True)
    parser.add_argument("--mode", choices=["serial", "parallel"], required=True)
    parser.add_argument("--max-parallel", type=int, default=2, help="mode parallel (default 2)")
    parser.add_argument("--worker", choices=["fake", "native"], default="fake")
    parser.add_argument("--model", help="model name at the provider (required for native)")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--fake-delay", type=float, default=2.0, help="seconds per fake task")
    parser.add_argument("--price-in", type=float, help="USD per 1M input tokens")
    parser.add_argument("--price-out", type=float, help="USD per 1M output tokens")
    parser.add_argument("--out", type=Path, default=Path("results.jsonl"))
    args = parser.parse_args(argv)
    if args.worker == "native" and not args.model:
        parser.error("--model is required with --worker native")
    if args.max_parallel < 1 or args.runs < 1:
        parser.error("--max-parallel and --runs must be at least 1")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    allowed = ", ".join(ALLOWED_PROGRAMS)
    print(f"Commands approved automatically: {allowed} (everything else is refused)")
    config = RunConfig(
        workload=WORKLOADS[args.workload],
        mode=args.mode,
        max_parallel=args.max_parallel,
        worker=args.worker,
        model=args.model,
        fake_delay=args.fake_delay,
        price_in=args.price_in,
        price_out=args.price_out,
    )
    records = asyncio.run(run_many(config, args.runs, args.out))
    walls = [r["wall_time_s"] for r in records if r["wall_time_s"] is not None]
    if walls:
        print(f"median wall time: {statistics.median(walls):.2f} s over {len(walls)} runs")
    return 0 if all(r["status"] == "done" for r in records) else 1


if __name__ == "__main__":
    sys.exit(main())
