"""One Git worktree per writing task, with a harness-computed diff (ADR-0004, ADR-0006)."""

from __future__ import annotations

import re
from pathlib import Path

from caecilia_harness import artifacts
from caecilia_harness.core.models import DiffArtifact, Workspace
from caecilia_harness.paths import worktrees_dir
from caecilia_harness.workspace.git import run_git

_TASK_ID = re.compile(r"[A-Za-z0-9_-]+")
_HARNESS_IDENTITY = ["-c", "user.name=caecilia-harness", "-c", "user.email=harness@localhost"]


class WorkspaceError(Exception):
    """Base class for workspace errors."""


class WorkspaceUnsupportedError(WorkspaceError):
    """The repository uses a feature the worktree backend does not support (e.g. submodules)."""


class InvalidTaskIdError(WorkspaceError, ValueError):
    """A task id that could escape the worktree root or is not a valid branch component."""


def branch_for(task_id: str) -> str:
    return f"caecilia/{task_id}"


def _check_task_id(task_id: str) -> None:
    if not _TASK_ID.fullmatch(task_id):
        raise InvalidTaskIdError(f"task id must match [A-Za-z0-9_-]+: {task_id!r}")


class GitWorktreeBackend:
    """`core.ports.WorkspaceBackend` that gives each task `<root>/<task_id>` on its own branch."""

    def __init__(self, repo_path: Path, root: Path | None = None) -> None:
        self.repo_path = repo_path
        # Resolved per instance, not as a default argument, so CAECILIA_HOME is read at use time.
        self.root = root if root is not None else worktrees_dir()

    async def create(self, task_id: str, base_commit: str) -> Workspace:
        """Add a worktree at `base_commit` on a new branch `caecilia/<task_id>`."""
        _check_task_id(task_id)
        base = (
            await run_git(["rev-parse", "--verify", f"{base_commit}^{{commit}}"], self.repo_path)
        ).text
        # ADR-0004: submodules are not supported by the MVP backend.
        listed = await run_git(["ls-tree", "--name-only", base, ".gitmodules"], self.repo_path)
        if listed.text:
            raise WorkspaceUnsupportedError(
                f"{self.repo_path} uses submodules (.gitmodules); "
                "the worktree backend does not support them yet"
            )
        path = self.root / task_id
        branch = branch_for(task_id)
        self.root.mkdir(parents=True, exist_ok=True)
        await run_git(["worktree", "add", "-b", branch, str(path), base], self.repo_path)
        return Workspace(task_id=task_id, path=str(path), branch=branch, base_commit=base)

    async def capture_diff(self, ws: Workspace) -> DiffArtifact:
        """Commit everything in the worktree (untracked included, ignored excluded) and store
        the patch against `ws.base_commit` as an artifact."""
        path = Path(ws.path)
        await run_git(["add", "-A"], path)
        staged = await run_git(["diff", "--cached", "--quiet"], path, check=False)
        if staged.returncode == 1:
            await run_git(
                [*_HARNESS_IDENTITY, "commit", "-q", "-m", f"caecilia: task {ws.task_id}"], path
            )
        head = (await run_git(["rev-parse", "HEAD"], path)).text

        span = f"{ws.base_commit}..{head}"
        patch = (await run_git(["diff", "--binary", span], path)).stdout
        names = (await run_git(["diff", "--name-only", "-z", span], path)).stdout
        files = [name for name in names.decode("utf-8").split("\0") if name]
        return DiffArtifact(
            task_id=ws.task_id,
            base_commit=ws.base_commit,
            head_commit=head,
            patch_ref=artifacts.put_bytes(patch, "patch"),
            files_changed=files,
            includes_untracked=True,
        )

    async def cleanup(self, ws: Workspace, keep: bool) -> None:
        """Remove the worktree and its branch, unless `keep` (e.g. for a rescue or inspection)."""
        if keep:
            return
        await run_git(["worktree", "remove", "--force", ws.path], self.repo_path)
        await run_git(["branch", "-D", ws.branch], self.repo_path)
