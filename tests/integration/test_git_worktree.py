"""Worktree per task and harness-computed diff capture (backlog M1-05)."""

from __future__ import annotations

from pathlib import Path

import pytest

from caecilia_harness.artifacts import read_bytes
from caecilia_harness.core.ports import WorkspaceBackend
from caecilia_harness.paths import worktrees_dir
from caecilia_harness.workspace.git_worktree import (
    GitWorktreeBackend,
    InvalidTaskIdError,
    WorkspaceUnsupportedError,
)
from tests.integration.conftest import git


@pytest.fixture
def backend(git_repo: Path) -> GitWorktreeBackend:
    return GitWorktreeBackend(git_repo)


def _is_dir(path: Path) -> bool:
    # Sync helper: ruff's ASYNC240 forbids blocking pathlib calls directly in async tests.
    return path.is_dir()


def _branches(repo: Path) -> list[str]:
    return git(repo, "branch", "--format=%(refname:short)").splitlines()


def test_satisfies_port(backend: GitWorktreeBackend) -> None:
    assert isinstance(backend, WorkspaceBackend)


def test_default_root_follows_caecilia_home(git_repo: Path, isolated_home: Path) -> None:
    assert GitWorktreeBackend(git_repo).root == worktrees_dir()
    assert worktrees_dir().is_relative_to(isolated_home)


async def test_create_makes_worktree_and_branch(
    backend: GitWorktreeBackend, git_repo: Path
) -> None:
    base = git(git_repo, "rev-parse", "HEAD")
    ws = await backend.create("t1", base)

    path = Path(ws.path)
    assert path == backend.root / "t1"
    assert (path / "README.md").is_file()
    assert ws.branch == "caecilia/t1"
    assert ws.base_commit == base
    assert "caecilia/t1" in _branches(git_repo)
    assert git(git_repo, "rev-parse", "caecilia/t1") == base


async def test_capture_diff_includes_untracked_and_skips_ignored(
    backend: GitWorktreeBackend, git_repo: Path
) -> None:
    base = git(git_repo, "rev-parse", "HEAD")
    ws = await backend.create("t2", base)
    path = Path(ws.path)
    (path / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    (path / "old.txt").unlink()
    (path / "new file.txt").write_text("brand new\n", encoding="utf-8")
    (path / "debug.log").write_text("ignored\n", encoding="utf-8")

    diff = await backend.capture_diff(ws)

    assert sorted(diff.files_changed) == ["new file.txt", "old.txt", "src/app.py"]
    assert diff.includes_untracked
    assert diff.base_commit == base
    assert diff.head_commit != base
    assert git(git_repo, "rev-parse", "caecilia/t2") == diff.head_commit
    assert git(git_repo, "log", "-1", "--format=%an <%ae>|%s", diff.head_commit) == (
        "caecilia-harness <harness@localhost>|caecilia: task t2"
    )

    patch = read_bytes(diff.patch_ref)
    assert b"print('changed')" in patch
    assert b"new file.txt" in patch
    assert b"debug.log" not in patch


async def test_capture_without_changes(backend: GitWorktreeBackend, git_repo: Path) -> None:
    base = git(git_repo, "rev-parse", "HEAD")
    ws = await backend.create("t3", base)
    diff = await backend.capture_diff(ws)
    assert diff.files_changed == []
    assert diff.head_commit == base
    assert git(git_repo, "rev-parse", "caecilia/t3") == base


async def test_cleanup_removes_worktree_and_branch(
    backend: GitWorktreeBackend, git_repo: Path
) -> None:
    ws = await backend.create("t4", git(git_repo, "rev-parse", "HEAD"))
    path = Path(ws.path)
    (path / "scratch.txt").write_text("x", encoding="utf-8")
    await backend.cleanup(ws, keep=False)
    assert not _is_dir(path)
    assert "caecilia/t4" not in _branches(git_repo)


async def test_cleanup_keep_leaves_everything(backend: GitWorktreeBackend, git_repo: Path) -> None:
    ws = await backend.create("t5", git(git_repo, "rev-parse", "HEAD"))
    path = Path(ws.path)
    await backend.cleanup(ws, keep=True)
    assert _is_dir(path)
    assert "caecilia/t5" in _branches(git_repo)


async def test_repo_with_submodules_is_rejected(
    backend: GitWorktreeBackend, git_repo: Path
) -> None:
    (git_repo / ".gitmodules").write_text('[submodule "x"]\n\tpath = x\n', encoding="utf-8")
    git(git_repo, "add", ".gitmodules")
    git(git_repo, "commit", "-q", "-m", "add submodules file")
    with pytest.raises(WorkspaceUnsupportedError):
        await backend.create("t6", git(git_repo, "rev-parse", "HEAD"))
    assert not (backend.root / "t6").exists()


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", "..", "", "t 1", "t1\n"])
async def test_unsafe_task_id_is_rejected(
    backend: GitWorktreeBackend, git_repo: Path, bad: str
) -> None:
    with pytest.raises(InvalidTaskIdError):
        await backend.create(bad, git(git_repo, "rev-parse", "HEAD"))
