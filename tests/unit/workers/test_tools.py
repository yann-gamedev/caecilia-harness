"""Native worker tools in the normal case (backlog M1-07)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from caecilia_harness.security.approval import ApprovalPolicy, ExecRequest
from caecilia_harness.workers.native.tools import TOOL_SPECS, ToolContext, dispatch
from caecilia_harness.workspace.exec_runner import ExecRunner

PY = sys.executable


class Approver:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.calls: list[ExecRequest] = []

    async def __call__(self, req: ExecRequest) -> bool:
        self.calls.append(req)
        return self.answer


def make_ctx(worktree: Path, approve: bool = True, **kw: Any) -> tuple[ToolContext, Approver]:
    approver = Approver(approve)
    runner = ExecRunner(worktree, ApprovalPolicy(), approver, None)
    return ToolContext(worktree=worktree, exec_runner=runner, **kw), approver


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / "src" / "app.py").write_bytes(b"x = 1\ny = 2\n")
    (wt / "README.md").write_text("# hi\n", encoding="utf-8")
    (wt / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    return wt


@pytest.fixture
def ctx(worktree: Path) -> ToolContext:
    return make_ctx(worktree)[0]


async def test_read_file(ctx: ToolContext) -> None:
    result = await dispatch(ctx, "read_file", {"path": "src/app.py"})
    assert result.ok
    assert result.content == "x = 1\ny = 2\n"
    assert result.untrusted


async def test_read_large_file_is_truncated(worktree: Path) -> None:
    (worktree / "big.txt").write_text("a" * 1000, encoding="utf-8")
    ctx, _ = make_ctx(worktree, max_read_bytes=100)
    result = await dispatch(ctx, "read_file", {"path": "big.txt"})
    assert result.ok
    assert result.content.startswith("a" * 100)
    assert "a" * 101 not in result.content
    assert "truncated" in result.content
    assert "1000" in result.content


async def test_read_missing_file(ctx: ToolContext) -> None:
    result = await dispatch(ctx, "read_file", {"path": "nope.txt"})
    assert not result.ok


async def test_write_file_creates_parents_with_lf(ctx: ToolContext, worktree: Path) -> None:
    result = await dispatch(ctx, "write_file", {"path": "new/dir/f.txt", "content": "a\nb\n"})
    assert result.ok
    assert (worktree / "new" / "dir" / "f.txt").read_bytes() == b"a\nb\n"


async def test_edit_file_replaces_unique_match(ctx: ToolContext, worktree: Path) -> None:
    result = await dispatch(
        ctx, "edit_file", {"path": "src/app.py", "old": "y = 2", "new": "y = 3"}
    )
    assert result.ok
    assert (worktree / "src" / "app.py").read_text(encoding="utf-8") == "x = 1\ny = 3\n"


@pytest.mark.parametrize(("old", "count"), [("missing", 0), (" = ", 2)])
async def test_edit_file_needs_exactly_one_match(
    ctx: ToolContext, worktree: Path, old: str, count: int
) -> None:
    before = (worktree / "src" / "app.py").read_bytes()
    result = await dispatch(ctx, "edit_file", {"path": "src/app.py", "old": old, "new": "z"})
    assert not result.ok
    assert str(count) in result.content
    assert (worktree / "src" / "app.py").read_bytes() == before


async def test_edit_file_keeps_crlf(ctx: ToolContext, worktree: Path) -> None:
    (worktree / "w.txt").write_bytes(b"one\r\ntwo\r\n")
    result = await dispatch(ctx, "edit_file", {"path": "w.txt", "old": "two", "new": "2"})
    assert result.ok
    assert (worktree / "w.txt").read_bytes() == b"one\r\n2\r\n"


async def test_list_dir(ctx: ToolContext) -> None:
    result = await dispatch(ctx, "list_dir", {})
    assert result.ok
    assert result.content.splitlines() == ["README.md", "src/"]
    sub = await dispatch(ctx, "list_dir", {"path": "src"})
    assert sub.content.splitlines() == ["app.py"]


async def test_run_summarises_result(worktree: Path) -> None:
    ctx, approver = make_ctx(worktree)
    cmd = [PY, "-c", "import sys; print('out'); sys.stderr.write('err')"]
    result = await dispatch(ctx, "run", {"argv": cmd, "cwd": "src"})
    assert result.ok
    assert "exit code: 0" in result.content
    assert "out" in result.content
    assert "err" in result.content
    assert approver.calls[0].cwd == (worktree / "src").resolve()


async def test_run_nonzero_exit_is_not_ok(ctx: ToolContext) -> None:
    result = await dispatch(ctx, "run", {"argv": [PY, "-c", "raise SystemExit(4)"]})
    assert not result.ok
    assert "exit code: 4" in result.content


async def test_run_denied_runs_nothing(worktree: Path) -> None:
    ctx, approver = make_ctx(worktree, approve=False)
    cmd = [PY, "-c", "open('ran.txt', 'w').close()"]
    result = await dispatch(ctx, "run", {"argv": cmd})
    assert not result.ok
    assert "denied" in result.content
    assert len(approver.calls) == 1
    assert not (worktree / "ran.txt").exists()


@pytest.mark.parametrize(
    ("name", "tool_input"),
    [
        ("delete_everything", {}),
        ("read_file", {}),
        ("read_file", {"path": 3}),
        ("read_file", {"path": "a", "extra": 1}),
        ("write_file", {"path": "a"}),
        ("edit_file", {"path": "a", "old": "x"}),
        ("run", {"argv": "echo hi"}),
        ("run", {"argv": []}),
    ],
)
async def test_unknown_tool_or_invalid_input(ctx: ToolContext, name: str, tool_input: Any) -> None:
    result = await dispatch(ctx, name, tool_input)
    assert not result.ok
    assert result.content


def test_tool_specs_are_object_schemas() -> None:
    expected_required = {
        "read_file": {"path"},
        "write_file": {"path", "content"},
        "edit_file": {"path", "old", "new"},
        "list_dir": set(),
        "run": {"argv"},
    }
    assert {spec.name for spec in TOOL_SPECS} == set(expected_required)
    for spec in TOOL_SPECS:
        schema = spec.input_schema
        assert schema["type"] == "object"
        assert set(schema.get("required", [])) == expected_required[spec.name]
        assert set(schema.get("required", [])) <= set(schema["properties"])
        assert schema.get("additionalProperties") is False
        assert spec.description
    run = next(spec for spec in TOOL_SPECS if spec.name == "run")
    assert run.input_schema["properties"]["argv"]["type"] == "array"
    assert run.input_schema["properties"]["argv"]["items"] == {"type": "string"}
