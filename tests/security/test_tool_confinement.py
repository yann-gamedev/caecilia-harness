"""Worker tools cannot leave the worktree or touch `.git` (backlog M1-07, zone Z3)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from caecilia_harness.security.approval import ApprovalPolicy, ExecRequest
from caecilia_harness.workers.native.tools import (
    PathOutsideWorktreeError,
    ToolContext,
    dispatch,
    resolve_in_worktree,
)
from caecilia_harness.workspace.exec_runner import ExecRunner


async def _yes(req: ExecRequest) -> bool:
    return True


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").mkdir()
    (wt / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("top secret\n", encoding="utf-8")
    return wt


@pytest.fixture
def ctx(worktree: Path) -> ToolContext:
    return ToolContext(
        worktree=worktree, exec_runner=ExecRunner(worktree, ApprovalPolicy(), _yes, None)
    )


ESCAPES = [
    "../secret.txt",
    "src/../../secret.txt",
    "/etc/passwd",
    "\\Windows\\win.ini",
    "C:\\Windows\\win.ini",
    "C:/Windows/win.ini",
    "C:secret.txt",
    "\\\\server\\share\\x",
    ".git/config",
    ".git",
    "sub/.git/config",
    ".GIT/config",
]


@pytest.mark.parametrize("path", ESCAPES)
def test_resolve_rejects_escapes(worktree: Path, path: str) -> None:
    with pytest.raises(PathOutsideWorktreeError):
        resolve_in_worktree(worktree, path)


@pytest.mark.parametrize("path", ESCAPES)
@pytest.mark.parametrize("tool", ["read_file", "write_file", "edit_file", "list_dir"])
async def test_tools_refuse_escapes(ctx: ToolContext, worktree: Path, tool: str, path: str) -> None:
    tool_input = {"path": path}
    if tool == "write_file":
        tool_input["content"] = "pwned"
    if tool == "edit_file":
        tool_input |= {"old": "[core]", "new": "pwned"}
    result = await dispatch(ctx, tool, tool_input)
    assert not result.ok
    assert "secret" not in result.content.replace("secret.txt", "")
    assert (worktree.parent / "secret.txt").read_text(encoding="utf-8") == "top secret\n"
    assert (worktree / ".git" / "config").read_text(encoding="utf-8") == "[core]\n"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows system file")
async def test_windows_system_file_is_refused(ctx: ToolContext) -> None:
    result = await dispatch(ctx, "read_file", {"path": "C:\\Windows\\win.ini"})
    assert not result.ok


async def test_run_cwd_cannot_escape(ctx: ToolContext) -> None:
    for cwd in ["..", ".git", "C:\\"]:
        result = await dispatch(ctx, "run", {"argv": [sys.executable, "-V"], "cwd": cwd})
        assert not result.ok, cwd


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create symlinks here: {exc}")


async def test_symlink_pointing_outside_is_refused(ctx: ToolContext, worktree: Path) -> None:
    _symlink_or_skip(worktree / "link.txt", worktree.parent / "secret.txt")
    _symlink_or_skip(worktree / "linkdir", worktree.parent)
    for path in ["link.txt", "linkdir/secret.txt"]:
        result = await dispatch(ctx, "read_file", {"path": path})
        assert not result.ok, path
        assert "top secret" not in result.content
    result = await dispatch(ctx, "write_file", {"path": "link.txt", "content": "pwned"})
    assert not result.ok
    assert (worktree.parent / "secret.txt").read_text(encoding="utf-8") == "top secret\n"
