"""M1 integration gate: rebase onto the latest target, run one check command, fast-forward
the target with compare-and-set (ADR-0005). The full merge queue arrives in M4."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from caecilia_harness.core.models import DiffArtifact, GateReport
from caecilia_harness.core.ports import BeforeMerge
from caecilia_harness.paths import worktrees_dir
from caecilia_harness.security.approval import ApprovalPolicy, ExecRequest
from caecilia_harness.workspace.exec_runner import ExecRunner
from caecilia_harness.workspace.git import run_git
from caecilia_harness.workspace.git_worktree import HARNESS_IDENTITY, validate_task_id

OUTPUT_TAIL_CHARS = 4000

Confirm = Callable[[GateReport], Awaitable[bool]]


async def _never_asked(req: ExecRequest) -> bool:  # pragma: no cover - policy is "allow"
    return False


def _tail(text: str) -> str:
    return text[-OUTPUT_TAIL_CHARS:]


class SimpleGate:
    """`core.ports.Gate` for one task at a time."""

    def __init__(
        self,
        repo_path: Path,
        check_command: list[str],
        confirm: Confirm,
        auto_merge: bool = False,
        timeout_s: float = 900,
        before_merge: BeforeMerge | None = None,
        root: Path | None = None,
    ) -> None:
        self.repo_path = repo_path
        self.check_command = check_command
        self.confirm = confirm
        self.auto_merge = auto_merge
        self.timeout_s = timeout_s
        self.before_merge = before_merge
        # Resolved per instance so CAECILIA_HOME is read at use time, not import time.
        self.root = root if root is not None else worktrees_dir()

    async def check(
        self,
        diff: DiffArtifact,
        target_branch: str,
        before_merge: BeforeMerge | None = None,
    ) -> GateReport:
        """Integrate `diff`. A per-call `before_merge` overrides the one given at construction."""
        hook = before_merge if before_merge is not None else self.before_merge
        task_id = diff.task_id
        validate_task_id(task_id)
        if not diff.files_changed:
            return GateReport(task_id=task_id, passed=True, output_tail="no changes to integrate")

        target_ref = f"refs/heads/{target_branch}"
        old = (await run_git(["rev-parse", "--verify", target_ref], self.repo_path)).text
        path = self.root / "_integrate" / task_id
        branch = f"caecilia/integrate/{task_id}"
        await self._remove_integration(path, branch)  # leftovers from a crashed attempt
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            await run_git(
                ["worktree", "add", "-b", branch, str(path), diff.head_commit], self.repo_path
            )
            return await self._integrate(diff, target_branch, target_ref, old, path, hook)
        finally:
            await self._remove_integration(path, branch)

    async def _integrate(
        self,
        diff: DiffArtifact,
        target: str,
        target_ref: str,
        old: str,
        path: Path,
        hook: BeforeMerge | None,
    ) -> GateReport:
        task_id = diff.task_id

        rebase = await run_git([*HARNESS_IDENTITY, "rebase", old], path, check=False)
        if rebase.returncode != 0:
            await run_git(["rebase", "--abort"], path, check=False)
            rescue = f"caecilia/rescue/{task_id}"
            await run_git(["branch", "-f", rescue, diff.head_commit], self.repo_path)
            return GateReport(
                task_id=task_id,
                passed=False,
                needs_human=True,
                checks={"rebase": False},
                output_tail=_tail(rebase.text + "\n" + rebase.stderr),
                rescue_branch=rescue,
            )

        command_ok, output = await self._run_check(path)
        report = GateReport(
            task_id=task_id,
            passed=command_ok,
            checks={"rebase": True, "command": command_ok},
            output_tail=_tail(output),
        )
        if not command_ok:
            return report
        if not self.auto_merge and not await self.confirm(report):
            return report

        new = (await run_git(["rev-parse", "HEAD"], path)).text
        return await self._fast_forward(report, target, target_ref, old, new, hook)

    async def _run_check(self, path: Path) -> tuple[bool, str]:
        """Run the user's check command in the rebased tree (scrubbed env, own process group)."""
        runner = ExecRunner(
            path,
            ApprovalPolicy(mode="allow"),
            _never_asked,
            sandbox=None,
            # The check command comes from the user (--check), not from a model.
            unsafe_no_sandbox=True,
            timeout_s=self.timeout_s,
        )
        try:
            result = await runner.run(list(self.check_command))
        except OSError as exc:
            return False, f"could not start check command: {exc}"
        output = result.stdout_tail + result.stderr_tail
        if result.timed_out:
            return False, output + f"\n[check command timed out after {self.timeout_s}s]"
        return result.exit_code == 0, output

    async def _fast_forward(
        self,
        report: GateReport,
        target: str,
        target_ref: str,
        old: str,
        new: str,
        hook: BeforeMerge | None,
    ) -> GateReport:
        checkout = await self._checked_out_at(target_ref)
        if checkout is not None:
            status = await run_git(["status", "--porcelain"], checkout)
            if status.text:
                return report.model_copy(
                    update={
                        "needs_human": True,
                        "output_tail": _tail(
                            report.output_tail
                            + f"\n{target} is checked out at {checkout} with uncommitted "
                            "changes; not merging"
                        ),
                    }
                )

        if hook is not None:
            await hook(old, new)

        if checkout is None:
            moved = await run_git(
                [
                    "update-ref",
                    "-m",
                    f"caecilia: merge task {report.task_id}",
                    target_ref,
                    new,
                    old,
                ],
                self.repo_path,
                check=False,
            )
            ok = moved.returncode == 0
        else:
            # update-ref would desync a checked-out working tree; fast-forward it instead,
            # but only if the branch is still where we found it (same CAS guarantee).
            current = (await run_git(["rev-parse", target_ref], self.repo_path)).text
            ok = (
                current == old
                and (
                    await run_git(["merge", "--ff-only", "-q", new], checkout, check=False)
                ).returncode
                == 0
            )
        if not ok:
            return report.model_copy(
                update={
                    "passed": False,
                    "output_tail": _tail(report.output_tail + "\ntarget moved"),
                }
            )
        return report.model_copy(update={"merged_commit": new})

    async def _checked_out_at(self, target_ref: str) -> Path | None:
        """Return the worktree where `target_ref` is checked out, if any."""
        listing = await run_git(["worktree", "list", "--porcelain"], self.repo_path)
        current: Path | None = None
        for line in listing.text.splitlines():
            if line.startswith("worktree "):
                current = Path(line.removeprefix("worktree "))
            elif line == f"branch {target_ref}":
                return current
        return None

    async def _remove_integration(self, path: Path, branch: str) -> None:
        await run_git(["worktree", "remove", "--force", str(path)], self.repo_path, check=False)
        await run_git(["worktree", "prune"], self.repo_path, check=False)
        await run_git(["branch", "-D", branch], self.repo_path, check=False)
