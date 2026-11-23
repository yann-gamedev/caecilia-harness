"""bench.report: medians and ranges recomputed from the JSONL (backlog M2-08)."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from bench.report import load, ratios, summarize, to_markdown


def _record(mode: str, wall: float, tokens_in: int, **extra: Any) -> dict[str, Any]:
    base = {
        "workload": "W1",
        "mode": mode,
        "max_parallel": 1 if mode == "serial" else 2,
        "status": "done",
        "wall_time_s": wall,
        "input_tokens": tokens_in,
        "output_tokens": tokens_in // 10,
        "cost_usd": None,
        "gate_failed": 0,
        "rework": 0,
        "needs_human": 0,
        "check_passed_after": True,
    }
    return base | extra


RECORDS = [
    _record("serial", 10.0, 1000),
    _record("serial", 14.0, 1200),
    _record("serial", 12.0, 1100, gate_failed=1, rework=1),
    _record("parallel", 6.0, 1300),
    _record("parallel", 9.0, 1500, status="needs_human", needs_human=1, check_passed_after=False),
]


def test_summary_matches_a_recomputation_from_the_records() -> None:
    serial, parallel = sorted(summarize(RECORDS), key=lambda r: r.mode != "serial")
    walls = [r["wall_time_s"] for r in RECORDS if r["mode"] == "serial"]
    assert serial.wall_s is not None
    assert (serial.wall_s.median, serial.wall_s.low, serial.wall_s.high) == (
        statistics.median(walls),
        min(walls),
        max(walls),
    )
    assert serial.runs == 3 and serial.gate_failed == 1 and serial.rework == 1
    assert parallel.wall_s is not None and parallel.wall_s.median == 7.5
    assert (parallel.not_done, parallel.needs_human, parallel.check_failed) == (1, 1, 1)
    assert serial.cost_usd is None  # no prices given


def test_ratios() -> None:
    ratio = ratios(summarize(RECORDS))["W1"]
    assert ratio["wall_time"] == round(7.5 / 12.0, 3)
    assert ratio["tokens"] == round((1400 + 140) / (1100 + 110), 3)


def test_markdown_from_a_jsonl_file(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in RECORDS) + "\n\n", encoding="utf-8")
    markdown = to_markdown(load(path))
    assert "| W1 | serial | 1 | 3 | 12.00 (10.00..14.00) |" in markdown
    assert "| W1 | parallel | 2 | 2 | 7.50 (6.00..9.00) |" in markdown
    assert "| W1 | 0.625 |" in markdown
