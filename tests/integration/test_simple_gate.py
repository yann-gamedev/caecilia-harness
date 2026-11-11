"""Simple integration gate: rebase, check command, CAS fast-forward (backlog M1-09)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from caecilia_harness.core.models import DiffArtifact, GateReport
from caecilia_harness.core.ports import Gate
from caecilia_harness.gate.simple import SimpleGate
from caecilia_harness.workspace.git_worktree import GitWorktreeBackend
from tests.integration.conftest import git

PY = sys.executable
PASS = [PY, "-c", "print('checks ok')"]
FAIL = [PY, "-c", "import sys; print('boom: 2 tests failed'); sys.exit(1)"]


async def _always(report: GateReport) -> bool:
    return True


async def _never(report: GateReport) -> bool:
    return False


def _commit(repo: Path, rel: str, content: str, message: str) -> str:
    (repo / rel).write_bytes(content.encode())
    git(repo, "add", rel)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


async def _task_diff(repo: Path, task_id: str, files: dict[str, str]) -> DiffArtifact:
    """Run a fake 'worker' in a task worktree and capture its diff."""
    backend = GitWorktreeBackend(repo)
    ws = await backend.create(task_id, git(repo, "rev-parse", "main"))
    for rel, content in files.items():
        (Path(ws.path) / rel).write_bytes(content.encode())
    return await backend.capture_diff(ws)


def _gate(repo: Path, check: list[str] = PASS, **kw: object) -> SimpleGate:
    kw.setdefault("confirm", _always)
    kw.setdefault("auto_merge", True)
    return SimpleGate(repo, check, **kw)  # type: ignore[arg-type]


def _leftovers(repo: Path) -> list[str]:
    branches = git(repo, "branch", "--list", "caecilia/integrate/*")
    worktrees = [
        line for line in git(repo, "worktree", "list").splitlines() if "_integrate" in line
    ]
    return [b for b in branches.splitlines() if b.strip()] + worktrees


@pytest.fixture(autouse=True)
def _no_leftovers(git_repo: Path):  # type: ignore[no-untyped-def]
    yield
    assert _leftovers(git_repo) == []


def test_satisfies_port(git_repo: Path) -> None:
    assert isinstance(_gate(git_repo), Gate)


async def test_valid_diff_is_fast_forwarded(git_repo: Path) -> None:
    old = git(git_repo, "rev-parse", "main")
    diff = await _task_diff(git_repo, "t1", {"feature.txt": "new feature\n"})
    report = await _gate(git_repo).check(diff, "main")

    assert report.passed and not report.needs_human
    assert report.checks == {"rebase": True, "command": True}
    assert report.merged_commit == git(git_repo, "rev-parse", "main")
    assert report.merged_commit != old
    assert git(git_repo, "show", "main:feature.txt") == "new feature"
    assert "checks ok" in report.output_tail
    # The checked-out main was fast-forwarded in place, so the working tree follows it.
    assert (git_repo / "feature.txt").read_text(encoding="utf-8") == "new feature\n"


async def test_empty_diff_passes_without_merge(git_repo: Path) -> None:
    old = git(git_repo, "rev-parse", "main")
    diff = await _task_diff(git_repo, "t0", {})
    report = await _gate(git_repo, FAIL).check(diff, "main")
    assert report.passed and report.merged_commit is None
    assert git(git_repo, "rev-parse", "main") == old


async def test_failing_check_leaves_target(git_repo: Path) -> None:
    old = git(git_repo, "rev-parse", "main")
    diff = await _task_diff(git_repo, "t2", {"feature.txt": "x\n"})
    report = await _gate(git_repo, FAIL).check(diff, "main")
    assert not report.passed
    assert report.checks["command"] is False
    assert "boom: 2 tests failed" in report.output_tail
    assert report.merged_commit is None
    assert git(git_repo, "rev-parse", "main") == old


async def test_check_runs_in_rebased_tree_with_scrubbed_env(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAECILIA_CANARY_TOKEN", "xyz")
    diff = await _task_diff(git_repo, "t3", {"feature.txt": "x\n"})
    script = (
        "import os, sys; "
        "assert os.environ.get('CAECILIA_CANARY_TOKEN') is None, 'secret leaked'; "
        "assert os.path.exists('feature.txt'), 'task change missing'"
    )
    report = await _gate(git_repo, [PY, "-c", script]).check(diff, "main")
    assert report.passed, report.output_tail


async def test_target_moved_without_conflict_is_rebased(git_repo: Path) -> None:
    diff = await _task_diff(git_repo, "t4", {"feature.txt": "task side\n"})
    other = _commit(git_repo, "other.txt", "landed meanwhile\n", "other work")
    report = await _gate(git_repo).check(diff, "main")

    assert report.passed and report.merged_commit
    assert git(git_repo, "show", "main:feature.txt") == "task side"
    assert git(git_repo, "show", "main:other.txt") == "landed meanwhile"
    assert git(git_repo, "rev-parse", f"{report.merged_commit}~1") == other


async def test_conflict_needs_human_and_keeps_rescue_branch(git_repo: Path) -> None:
    diff = await _task_diff(git_repo, "t5", {"README.md": "# task version\n"})
    moved = _commit(git_repo, "README.md", "# main version\n", "edit readme on main")
    report = await _gate(git_repo).check(diff, "main")

    assert not report.passed and report.needs_human
    assert report.checks.get("rebase") is False
    assert report.rescue_branch == "caecilia/rescue/t5"
    assert git(git_repo, "rev-parse", "caecilia/rescue/t5") == diff.head_commit
    assert git(git_repo, "rev-parse", "main") == moved


async def test_declined_confirmation_does_not_merge(git_repo: Path) -> None:
    old = git(git_repo, "rev-parse", "main")
    seen: list[GateReport] = []

    async def decline(report: GateReport) -> bool:
        seen.append(report)
        return False

    diff = await _task_diff(git_repo, "t6", {"feature.txt": "x\n"})
    report = await _gate(git_repo, confirm=decline, auto_merge=False).check(diff, "main")
    assert report.passed and report.merged_commit is None
    assert len(seen) == 1 and seen[0].passed
    assert git(git_repo, "rev-parse", "main") == old


async def test_auto_merge_skips_confirmation(git_repo: Path) -> None:
    diff = await _task_diff(git_repo, "t7", {"feature.txt": "x\n"})
    report = await _gate(git_repo, confirm=_never, auto_merge=True).check(diff, "main")
    assert report.merged_commit is not None


@pytest.mark.parametrize("target", ["main", "release"], ids=["checked-out", "not-checked-out"])
async def test_target_moving_before_merge_fails_cas(git_repo: Path, target: str) -> None:
    git(git_repo, "branch", "release", "main")
    diff = await _task_diff(git_repo, "t8", {"feature.txt": "x\n"})
    moved: list[str] = []

    async def move_target(old: str, new: str) -> None:
        # Someone else lands a commit on the target between the snapshot and the merge.
        tree = git(git_repo, "rev-parse", f"{old}^{{tree}}")
        sneaky = git(git_repo, "commit-tree", tree, "-p", old, "-m", "sneaky")
        git(git_repo, "update-ref", f"refs/heads/{target}", sneaky)
        if target == "main":
            git(git_repo, "reset", "-q", "--hard", sneaky)
        moved.append(sneaky)

    report = await _gate(git_repo, before_merge=move_target).check(diff, target)
    assert not report.passed
    assert report.merged_commit is None
    assert "target moved" in report.output_tail
    assert git(git_repo, "rev-parse", target) == moved[0]


async def test_not_checked_out_target_is_moved_by_update_ref(git_repo: Path) -> None:
    git(git_repo, "branch", "release", "main")
    head_before = git(git_repo, "rev-parse", "HEAD")
    diff = await _task_diff(git_repo, "t9", {"feature.txt": "x\n"})
    report = await _gate(git_repo).check(diff, "release")
    assert report.merged_commit == git(git_repo, "rev-parse", "release")
    assert git(git_repo, "rev-parse", "HEAD") == head_before  # main untouched


async def test_dirty_checked_out_target_needs_human(git_repo: Path) -> None:
    old = git(git_repo, "rev-parse", "main")
    diff = await _task_diff(git_repo, "t10", {"feature.txt": "x\n"})
    (git_repo / "README.md").write_bytes(b"# uncommitted edit\n")
    called: list[tuple[str, str]] = []

    async def record(old_sha: str, new_sha: str) -> None:
        called.append((old_sha, new_sha))

    report = await _gate(git_repo, before_merge=record).check(diff, "main")
    assert report.needs_human and report.merged_commit is None
    assert git(git_repo, "rev-parse", "main") == old
    assert (git_repo / "README.md").read_bytes() == b"# uncommitted edit\n"
    assert called == []


async def test_before_merge_called_once_before_target_moves(git_repo: Path) -> None:
    old = git(git_repo, "rev-parse", "main")
    diff = await _task_diff(git_repo, "t11", {"feature.txt": "x\n"})
    calls: list[tuple[str, str, str]] = []

    async def record(old_sha: str, new_sha: str) -> None:
        calls.append((old_sha, new_sha, git(git_repo, "rev-parse", "main")))

    report = await _gate(git_repo, before_merge=record).check(diff, "main")
    assert calls == [(old, report.merged_commit, old)]
