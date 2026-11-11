"""Exec stays inside the worktree and never sees secrets (backlog M1-06, ADR-0008)."""

from __future__ import annotations

import shutil
import sys
import textwrap
from pathlib import Path

import pytest

from caecilia_harness.security.approval import ApprovalPolicy, ExecRequest
from caecilia_harness.workspace.exec_runner import ExecRefusedError, ExecRunner
from caecilia_harness.workspace.sandbox import detect_sandbox

PY = sys.executable

needs_bwrap = pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="needs Linux with bubblewrap",
)


async def _yes(req: ExecRequest) -> bool:
    return True


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    wt.mkdir()
    return wt


def _runner(worktree: Path) -> ExecRunner:
    return ExecRunner(worktree, ApprovalPolicy(), _yes, None)


def _sandboxed_runner(worktree: Path) -> ExecRunner:
    sandbox = detect_sandbox()
    assert sandbox is not None
    return ExecRunner(worktree, ApprovalPolicy(), _yes, sandbox)


async def test_child_does_not_see_secret_env(
    worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAECILIA_CANARY_TOKEN", "xyz")
    monkeypatch.setenv("MY_API_KEY", "k")
    script = (
        "import os; print(os.environ.get('CAECILIA_CANARY_TOKEN'), "
        "os.environ.get('MY_API_KEY'), 'PATH' in os.environ)"
    )
    result = await _runner(worktree).run([PY, "-c", script])
    assert result.stdout_tail.split() == ["None", "None", "True"]


def _outside_targets(wt: Path) -> dict[str, Path]:
    return {
        "dotdot": wt / "..",
        "sibling": wt / ".." / "wt-sibling",
        "parent": wt.parent,
        "root": Path(wt.anchor),
        "relative-dotdot": Path(".."),
    }


@pytest.mark.parametrize("which", list(_outside_targets(Path("x"))))
async def test_cwd_outside_worktree_is_refused(worktree: Path, which: str) -> None:
    (worktree.parent / "wt-sibling").mkdir(exist_ok=True)
    target = _outside_targets(worktree)[which]
    with pytest.raises(ExecRefusedError):
        await _runner(worktree).run([PY, "-c", "print(1)"], cwd=target)


async def test_relative_cwd_inside_worktree_is_allowed(worktree: Path) -> None:
    (worktree / "pkg").mkdir()
    result = await _runner(worktree).run(
        [PY, "-c", "import os; print(os.path.basename(os.getcwd()))"], cwd=Path("pkg")
    )
    assert result.stdout_tail.strip() == "pkg"


@pytest.mark.linux_only
@needs_bwrap
async def test_bwrap_blocks_writes_outside_worktree(worktree: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    home_file = Path.home() / ".caecilia-sandbox-probe"
    script = textwrap.dedent(
        """
        import sys
        for target in sys.argv[1:]:
            try:
                open(target, "w").write("x")
            except OSError:
                pass
        open("inside.txt", "w").write("ok")
        """
    )
    result = await _sandboxed_runner(worktree).run([PY, "-c", script, str(outside), str(home_file)])
    assert result.sandboxed
    assert result.exit_code == 0, result.stderr_tail
    assert (worktree / "inside.txt").read_text() == "ok"
    # / is read-only and /tmp is a private tmpfs: nothing may reach the host.
    assert not outside.exists()
    assert not home_file.exists()


@pytest.mark.linux_only
@needs_bwrap
async def test_bwrap_blocks_outbound_network(worktree: Path) -> None:
    # 192.0.2.1 is TEST-NET-1 (RFC 5737) and never routed, so no real traffic happens even if
    # the sandbox were broken. With --unshare-net only loopback exists: connect fails at once.
    script = textwrap.dedent(
        """
        import socket
        s = socket.socket()
        s.settimeout(3)
        try:
            s.connect(("192.0.2.1", 80))
            print("connected")
        except OSError as exc:
            print("blocked", exc.errno)
        """
    )
    result = await _sandboxed_runner(worktree).run([PY, "-c", script])
    assert result.stdout_tail.startswith("blocked"), result.stdout_tail
