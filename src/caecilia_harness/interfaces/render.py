"""Text formatting for the CLI: one line per durable event, plus status and run tables.

`plain=True` guarantees no ANSI colour codes and no box-drawing characters (logs, CI).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import typer

from caecilia_harness.bootstrap import GraphView, RunStatus, RunSummary
from caecilia_harness.core.models import Event
from caecilia_harness.planning.plan import Plan, plan_id

_COLORS = {
    "run.": typer.colors.MAGENTA,
    "task.state_changed": typer.colors.CYAN,
    "task.lease": typer.colors.BRIGHT_BLACK,
    "worker.": typer.colors.BLUE,
    "gate.passed": typer.colors.GREEN,
    "gate.failed": typer.colors.RED,
    "gate.": typer.colors.YELLOW,
    "approval.": typer.colors.YELLOW,
}


def _short(sha: Any) -> str:
    return str(sha)[:8] if sha else "-"


def _last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def event_summary(event: Event) -> str:
    """Short human summary of an event's payload."""
    p = event.payload
    match event.type:
        case "graph.revised":
            tasks, edges = p.get("tasks") or [], p.get("edges") or []
            summary = f"revisi {p.get('revision')}: {len(tasks)} task, {len(edges)} dependensi"
            ops = [str(op.get("op")) for op in p.get("ops") or []]
            return summary + (f" ({', '.join(ops)})" if ops else "")
        case "task.blockers_changed":
            return f"blocked_by: {', '.join(p.get('blocked_by') or []) or '-'}"
        case "run.reopened":
            return "dibuka lagi setelah revisi graf"
        case "run.created":
            return f"goal: {p.get('goal', '')}"
        case "run.finished":
            return f"status: {p.get('status', '')}"
        case "task.created":
            return str((p.get("spec") or {}).get("title", ""))
        case "task.state_changed":
            reason = f" ({p['reason']})" if p.get("reason") else ""
            return f"{p.get('from_state')} -> {p.get('to_state')}{reason}"
        case "task.lease_acquired" | "task.lease_released" | "task.lease_expired":
            return f"holder {p.get('holder')}"
        case "worker.started":
            return f"{p.get('worker')} attempt {p.get('attempt')} di {p.get('path')}"
        case "worker.result":
            if p.get("success"):
                files = (p.get("diff") or {}).get("files_changed") or []
                return f"sukses, {len(files)} file berubah"
            return f"gagal: {p.get('error')}"
        case "gate.started":
            return f"attempt {p.get('attempt')}"
        case "gate.merging":
            return f"{_short(p.get('old'))} -> {_short(p.get('new'))}"
        case "gate.passed":
            report = p.get("report") or {}
            merged = report.get("merged_commit") or p.get("merged_commit")
            return f"merged {_short(merged)}" if merged else "lulus, tidak di-merge"
        case "gate.failed":
            report = p.get("report") or {}
            return _last_line(str(report.get("output_tail", ""))) or "gagal"
        case "approval.requested":
            argv = ((p.get("action") or {}).get("detail") or {}).get("argv") or []
            return " ".join(str(a) for a in argv)
        case "approval.decided":
            return "diizinkan" if p.get("allowed") else "ditolak"
        case "usage.recorded":
            return f"in {p.get('input_tokens', 0)} / out {p.get('output_tokens', 0)} token"
        case _:
            return ""


def _style(text: str, event_type: str, plain: bool) -> str:
    if plain:
        return text
    for prefix, color in _COLORS.items():
        if event_type.startswith(prefix):
            return typer.style(text, fg=color)
    return text


def format_event(event: Event, plain: bool, run_id: str = "") -> str:
    """`HH:MM:SS  #seq  [plan task id]  type  stream  summary` (local time)."""
    clock = event.ts.astimezone().strftime("%H:%M:%S")
    kind = _style(f"{event.type:<20}", event.type, plain)
    scope, _, identity = event.stream.partition(":")
    task = plan_id(identity, run_id) if scope in {"task", "approval"} else "-"
    return (
        f"{clock}  #{event.seq:<4} [{task}] {kind} {event.stream:<22} {event_summary(event)}"
    ).rstrip()


def _table(header: Sequence[str], rows: Sequence[Sequence[str]], plain: bool) -> list[str]:
    widths = [max(len(str(c)) for c in col) for col in zip(header, *rows, strict=False)]
    lines = ["  ".join(f"{h:<{w}}" for h, w in zip(header, widths, strict=True)).rstrip()]
    if not plain:
        lines[0] = typer.style(lines[0], bold=True)
    for row in rows:
        lines.append("  ".join(f"{c:<{w}}" for c, w in zip(row, widths, strict=True)).rstrip())
    return lines


def format_status(status: RunStatus, plain: bool) -> list[str]:
    run = status.run
    lines = [
        f"Run {run.id}  [{run.status}]  {run.created_at.astimezone():%Y-%m-%d %H:%M}",
        f"Goal: {run.goal}",
        "",
    ]
    rows = [
        [
            task.id,
            task.state,
            ", ".join(task.blocked_by) or "-",
            str(task.attempts),
            task.worker,
            task.summary,
        ]
        for task in status.tasks
    ]
    header = ["TASK", "STATE", "BLOCKED_BY", "ATTEMPTS", "WORKER", "RINGKASAN"]
    return lines + _table(header, rows, plain)


def format_graph(view: GraphView, plain: bool) -> list[str]:
    """A run's graph in topological order, with its current revision."""
    rows = [
        [
            task.plan_id,
            task.state,
            ", ".join(task.blocked_by) or "-",
            ", ".join(task.write_set) or "**",
        ]
        for task in view.tasks
    ]
    header = [f"Run {view.run_id}  revisi graf {view.revision}", ""]
    return header + _table(["TASK", "STATE", "BLOCKED_BY", "WRITE_SET"], rows, plain)


def format_plan(plan: Plan, plain: bool) -> list[str]:
    """Task table for a proposed plan: id, title, blockers and write set."""
    rows = [
        [
            task.id,
            task.title if len(task.title) <= 40 else task.title[:37] + "...",
            ", ".join(task.blocked_by) or "-",
            ", ".join(task.write_set) or "**",
        ]
        for task in plan.tasks
    ]
    return _table(["TASK", "JUDUL", "BLOCKED_BY", "WRITE_SET"], rows, plain)


def format_runs(runs: Sequence[RunSummary], plain: bool) -> list[str]:
    rows = [
        [
            run.id,
            run.status,
            f"{run.created_at.astimezone():%Y-%m-%d %H:%M}",
            run.goal if len(run.goal) <= 50 else run.goal[:47] + "...",
        ]
        for run in runs
    ]
    return _table(["RUN", "STATUS", "WAKTU", "GOAL"], rows, plain)
