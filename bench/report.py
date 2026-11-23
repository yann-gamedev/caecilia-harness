"""Turn benchmark JSONL into Markdown tables: median and range per workload x mode.

python -m bench.report bench/results/2026-11-30.jsonl --out docs/benchmarks/2026-11-30.md
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Spread:
    median: float
    low: float
    high: float

    @classmethod
    def of(cls, values: Sequence[float]) -> Spread | None:
        if not values:
            return None
        return cls(float(statistics.median(values)), float(min(values)), float(max(values)))

    def show(self, digits: int = 1) -> str:
        return f"{self.median:.{digits}f} ({self.low:.{digits}f}..{self.high:.{digits}f})"


@dataclass(frozen=True)
class Row:
    workload: str
    mode: str
    max_parallel: int
    runs: int
    wall_s: Spread | None
    input_tokens: Spread | None
    output_tokens: Spread | None
    cost_usd: Spread | None
    gate_failed: int
    rework: int
    needs_human: int
    not_done: int
    check_failed: int


def load(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def summarize(records: Sequence[dict[str, Any]]) -> list[Row]:
    """One row per (workload, mode, max_parallel), sorted for stable output."""
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for record in records:
        key = (str(record["workload"]), str(record["mode"]), int(record["max_parallel"]))
        groups.setdefault(key, []).append(record)
    rows = []
    for (workload, mode, max_parallel), group in sorted(groups.items()):

        def values(field: str, group: list[dict[str, Any]] = group) -> list[float]:
            return [float(r[field]) for r in group if r.get(field) is not None]

        rows.append(
            Row(
                workload=workload,
                mode=mode,
                max_parallel=max_parallel,
                runs=len(group),
                wall_s=Spread.of(values("wall_time_s")),
                input_tokens=Spread.of(values("input_tokens")),
                output_tokens=Spread.of(values("output_tokens")),
                cost_usd=Spread.of(values("cost_usd")),
                gate_failed=sum(int(r.get("gate_failed", 0)) for r in group),
                rework=sum(int(r.get("rework", 0)) for r in group),
                needs_human=sum(int(r.get("needs_human", 0)) for r in group),
                not_done=sum(1 for r in group if r.get("status") != "done"),
                check_failed=sum(1 for r in group if r.get("check_passed_after") is False),
            )
        )
    return rows


def ratios(rows: Sequence[Row]) -> dict[str, dict[str, float | None]]:
    """Per workload: parallel / serial for median wall time and median total tokens."""
    result: dict[str, dict[str, float | None]] = {}
    by_workload: dict[str, dict[str, Row]] = {}
    for row in rows:
        by_workload.setdefault(row.workload, {})[row.mode] = row
    for workload, modes in sorted(by_workload.items()):
        serial, parallel = modes.get("serial"), modes.get("parallel")
        if serial is None or parallel is None:
            continue

        def ratio(a: Spread | None, b: Spread | None) -> float | None:
            return round(a.median / b.median, 3) if a and b and b.median else None

        def tokens(row: Row) -> Spread | None:
            if row.input_tokens is None or row.output_tokens is None:
                return None
            total = row.input_tokens.median + row.output_tokens.median
            return Spread(total, total, total)

        result[workload] = {
            "wall_time": ratio(parallel.wall_s, serial.wall_s),
            "tokens": ratio(tokens(parallel), tokens(serial)),
        }
    return result


def _show(spread: Spread | None, digits: int = 1) -> str:
    return spread.show(digits) if spread else "-"


def to_markdown(records: Sequence[dict[str, Any]]) -> str:
    rows = summarize(records)
    lines = [
        "| Workload | Mode | max_parallel | Run | Wall time s | Token in | Token out | Biaya USD "
        "| gate.failed | REWORK | NEEDS_HUMAN | Tidak done | Check gagal |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(
            f"| {r.workload} | {r.mode} | {r.max_parallel} | {r.runs} | {_show(r.wall_s, 2)} "
            f"| {_show(r.input_tokens, 0)} | {_show(r.output_tokens, 0)} | {_show(r.cost_usd, 4)} "
            f"| {r.gate_failed} | {r.rework} | {r.needs_human} | {r.not_done} | {r.check_failed} |"
        )
    lines += [
        "",
        "Nilai: median (min..maks). Rasio = median paralel / median serial "
        "(< 1 berarti paralel lebih kecil).",
        "",
        "| Workload | Rasio wall time | Rasio token |",
        "| --- | --- | --- |",
    ]
    for workload, ratio in ratios(rows).items():
        wall, tokens = (
            "-" if value is None else str(value) for value in (ratio["wall_time"], ratio["tokens"])
        )
        lines.append(f"| {workload} | {wall} | {tokens} |")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Markdown report from benchmark JSONL")
    parser.add_argument("results", type=Path)
    parser.add_argument("--out", type=Path, help="write the Markdown here instead of stdout")
    args = parser.parse_args(argv)
    markdown = to_markdown(load(args.results))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(markdown, encoding="utf-8")
    else:
        sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
