"""One working session on a repo: propose a plan, start it, resume, look at runs (CLI-02).

Every frontend uses this flow: the interactive `caecilia` session, `caecilia -p`, and later the
full-screen TUI and the desktop GUI (ADR-0012, ADR-0015). Nothing here reads from or prints to a
terminal; questions go through the `UserPrompter` the frontend hands in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from caecilia_harness import bootstrap
from caecilia_harness.bootstrap import (
    AppError,
    GraphView,
    PreparedRun,
    RunReport,
    RunStatus,
    RunSummary,
)
from caecilia_harness.core.models import Action, GateReport, Usage
from caecilia_harness.core.ports import UserPrompter
from caecilia_harness.planning.plan import Plan, dump_plan, load_plan, parse_plan, single_task_plan
from caecilia_harness.project_config import (
    MissingOptionError,
    OptionSource,
    RunOptions,
)
from caecilia_harness.security import keys


class AllowListPrompter:
    """Never asks anyone: for `caecilia -p`, scripts and CI.

    An exec request is approved only when its argv starts with one of `allow` (from `--allow-cmd`
    or `allow_commands` in `.caecilia.toml`); everything else is refused and kept in `refused`
    (the orchestrator also records every decision as an event). Merges are never confirmed here:
    they only happen with `--auto-merge`, where the gate does not ask.
    """

    def __init__(self, allow: list[list[str]]) -> None:
        self.allow = [tuple(prefix) for prefix in allow if prefix]
        self.refused: list[list[str]] = []

    async def ask_approval(self, action: Action) -> bool:
        argv = [str(arg) for arg in action.detail.get("argv", [])]
        if argv and any(tuple(argv[: len(prefix)]) == prefix for prefix in self.allow):
            return True
        self.refused.append(argv)
        return False

    async def confirm_merge(self, report: GateReport) -> bool:
        return False


@dataclass(frozen=True)
class OptionView:
    """One effective option and where it came from (`/config`)."""

    name: str
    value: str
    source: OptionSource


@dataclass
class Session:
    """The state of one session: the repo, the effective options and the last plan.

    The last plan stays in memory; a file is only written by `save`.
    """

    repo: Path
    options: RunOptions
    prompter: UserPrompter
    sources: dict[str, OptionSource] = field(default_factory=dict)
    auto_merge: bool = False
    unsafe_no_sandbox: bool = False
    max_tasks: int = 8
    last_plan: Plan | None = None
    #: Tokens the planner used for `last_plan` (zero for a plan made without a model).
    last_plan_usage: Usage = field(default_factory=Usage)

    # --- plans -----------------------------------------------------------------------------

    async def propose(self, goal: str) -> Plan:
        """A plan for `goal`: from the planner model, or one task when the worker is fake."""
        if not goal.strip():
            raise AppError("goal tidak boleh kosong")
        if self.options.worker == "fake":
            plan, usage = single_task_plan(goal.strip()), Usage()
        else:
            if not self.options.model:
                raise MissingOptionError(
                    "--model", "model", "wajib untuk planner dan worker native"
                )
            result = await bootstrap.propose_plan(
                goal.strip(), self.repo, self.options.model, max_tasks=self.max_tasks
            )
            plan, usage = result.plan, result.usage
        self.last_plan, self.last_plan_usage = plan, usage
        return plan

    def load(self, path: Path) -> Plan:
        """Read a plan file and make it the session's plan (`PlanError` lists every problem)."""
        self.last_plan, self.last_plan_usage = load_plan(path), Usage()
        return self.last_plan

    def replace_plan(self, text: str, source: str = "plan") -> Plan:
        """Validate edited plan text; the previous plan stays if it is broken (`PlanError`)."""
        self.last_plan = parse_plan(text, source)
        return self.last_plan

    def plan_text(self) -> str:
        return dump_plan(self.require_plan())

    def save(self, path: Path, force: bool = False) -> Path:
        """Write the last plan to `path` (the only time the session writes a plan file)."""
        text = self.plan_text()
        if path.exists() and not force:
            raise AppError(f"file sudah ada: {path} (pakai --force untuk menimpa)")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def require_plan(self) -> Plan:
        if self.last_plan is None:
            raise AppError("belum ada plan: ketik goal, `/plan <goal>`, atau `/run <file>`")
        return self.last_plan

    # --- runs ------------------------------------------------------------------------------

    async def start(self, plan: Plan | None = None) -> PreparedRun:
        """Create a run from `plan` (default: the last plan). Always `close()` the result."""
        chosen = plan or self.require_plan()
        check = self.options.require_check()
        if self.options.worker == "native" and not self.options.model:
            raise MissingOptionError("--model", "model", "wajib untuk worker native")
        return await bootstrap.prepare_plan_run(
            chosen,
            self.repo,
            check,
            self.prompter,
            worker_kind=self.options.worker,
            target=self.options.target,
            auto_merge=self.auto_merge,
            unsafe_no_sandbox=self.unsafe_no_sandbox,
            model=self.options.model,
            max_parallel=self.options.max_parallel,
            provider_slots=self.options.provider_slots,
            exec_slots=self.options.exec_slots,
        )

    async def resume(self, run_id: str | None = None) -> PreparedRun:
        """Recover and continue a run of this repo (the latest unfinished one by default)."""
        return await bootstrap.resume_run(
            self.prompter, run_id, self.unsafe_no_sandbox, repo=self.repo
        )

    def status(self, run_id: str | None = None) -> RunStatus | None:
        return bootstrap.get_run_status(run_id, repo=self.repo)

    def graph(self, run_id: str | None = None) -> GraphView:
        if run_id is None:
            latest = self.runs(limit=1)
            if not latest:
                raise AppError("belum ada run di repo ini")
            run_id = latest[0].id
        return bootstrap.get_graph(run_id)

    def runs(self, limit: int = 20) -> list[RunSummary]:
        return bootstrap.list_runs(limit, repo=self.repo)

    def unfinished_runs(self) -> list[RunSummary]:
        return [run for run in bootstrap.list_runs(1000, repo=self.repo) if run.status == "running"]

    def report(self, run_id: str) -> RunReport:
        return bootstrap.run_report(run_id)

    # --- what the session runs with --------------------------------------------------------

    def key_present(self) -> bool | None:
        """Whether the provider's API key exists; None when the worker needs none.

        Only presence is ever reported, never the key.
        """
        if self.options.worker != "native":
            return None
        return keys.key_source(self.options.provider) is not None

    def config_view(self) -> list[OptionView]:
        """The effective options and their origin (CLI, `.caecilia.toml`, or default)."""
        o = self.options
        values: dict[str, object] = {
            "check": " ".join(o.check) if o.check else None,
            "worker": o.worker,
            "provider": o.provider,
            "model": o.model,
            "max_parallel": o.max_parallel,
            "provider_slots": o.provider_slots,
            "exec_slots": o.exec_slots,
            "target": o.target,
            "allow_commands": "; ".join(" ".join(p) for p in o.allow_commands) or None,
        }
        return [
            OptionView(
                name, "-" if value is None else str(value), self.sources.get(name, "default")
            )
            for name, value in values.items()
        ]
