"""Tools the native worker's model may call (zones Z2/Z3).

Every path is confined to the task worktree and `.git` is off limits. Tool output is data,
never instructions: results carry `untrusted=True` unless the text was written by the harness.
Mistakes come back as `ToolResult(ok=False)` so the model can correct itself; they never raise.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from caecilia_harness.core.models import ToolSpec
from caecilia_harness.workspace.exec_runner import ExecRefusedError, ExecRunner


class PathOutsideWorktreeError(ValueError):
    """A tool path that is absolute, escapes the worktree, or points into `.git`."""


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    content: str
    untrusted: bool = True


@dataclass(frozen=True)
class ToolContext:
    worktree: Path
    exec_runner: ExecRunner
    max_read_bytes: int = 200_000


def _harness_error(message: str) -> ToolResult:
    return ToolResult(ok=False, content=message, untrusted=False)


def resolve_in_worktree(worktree: Path, rel_path: str) -> Path:
    """Return the absolute path for `rel_path` inside `worktree`, or raise.

    Rejects absolute paths and Windows drives/UNC roots on every OS (a model may emit either),
    `..` escapes, symlinks resolving outside the worktree, and anything under `.git`.
    """
    if PurePosixPath(rel_path).anchor or PureWindowsPath(rel_path).anchor:
        raise PathOutsideWorktreeError(f"path must be relative to the worktree: {rel_path!r}")
    if sys.platform == "win32" and ":" in rel_path:
        # Blocks drive-relative paths and NTFS alternate data streams ("file:stream").
        raise PathOutsideWorktreeError(f"':' is not allowed in paths: {rel_path!r}")
    root = worktree.resolve()
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root):
        raise PathOutsideWorktreeError(f"path leaves the worktree: {rel_path!r}")
    parts = target.relative_to(root).parts
    if any(part.casefold() == ".git" for part in parts):
        raise PathOutsideWorktreeError(f"the .git folder is off limits: {rel_path!r}")
    return target


# --- inputs (also the source of each tool's JSON Schema) -----------------------------------


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReadFileInput(_Input):
    path: str = Field(description="File path relative to the worktree root.")


class WriteFileInput(_Input):
    path: str = Field(description="File path relative to the worktree root.")
    content: str = Field(description="Full new file content (UTF-8).")


class EditFileInput(_Input):
    path: str = Field(description="File path relative to the worktree root.")
    old: str = Field(description="Exact text to replace; must occur exactly once.")
    new: str = Field(description="Replacement text.")


class ListDirInput(_Input):
    path: str = Field(default=".", description="Folder relative to the worktree root.")


class RunInput(_Input):
    argv: list[str] = Field(min_length=1, description="Program and arguments; no shell.")
    cwd: str | None = Field(default=None, description="Working folder relative to the worktree.")


# --- tool implementations ------------------------------------------------------------------


def _read_file(ctx: ToolContext, args: ReadFileInput) -> ToolResult:
    path = resolve_in_worktree(ctx.worktree, args.path)
    if not path.is_file():
        return _harness_error(f"not a file: {args.path}")
    size = path.stat().st_size
    with path.open("rb") as fh:
        data = fh.read(ctx.max_read_bytes)
    text = data.decode("utf-8", errors="replace")
    if size > ctx.max_read_bytes:
        text += f"\n[... truncated: showing first {ctx.max_read_bytes} of {size} bytes]"
    return ToolResult(ok=True, content=text)


def _write_file(ctx: ToolContext, args: WriteFileInput) -> ToolResult:
    path = resolve_in_worktree(ctx.worktree, args.path)
    if path.is_dir():
        return _harness_error(f"is a folder: {args.path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args.content, encoding="utf-8", newline="\n")
    return ToolResult(ok=True, content=f"wrote {args.path}", untrusted=False)


def _edit_file(ctx: ToolContext, args: EditFileInput) -> ToolResult:
    path = resolve_in_worktree(ctx.worktree, args.path)
    if not path.is_file():
        return _harness_error(f"not a file: {args.path}")
    if not args.old:
        return _harness_error("'old' must not be empty")
    try:
        # newline="" keeps the file's own line endings (CRLF stays CRLF).
        with path.open(encoding="utf-8", newline="") as fh:
            text = fh.read()
    except UnicodeDecodeError:
        return _harness_error(f"not valid UTF-8, refusing to edit: {args.path}")
    count = text.count(args.old)
    if count != 1:
        return _harness_error(f"'old' must occur exactly once in {args.path}, found {count}")
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text.replace(args.old, args.new, 1))
    return ToolResult(ok=True, content=f"edited {args.path}", untrusted=False)


def _list_dir(ctx: ToolContext, args: ListDirInput) -> ToolResult:
    path = resolve_in_worktree(ctx.worktree, args.path)
    if not path.is_dir():
        return _harness_error(f"not a folder: {args.path}")
    names = sorted(
        entry.name + ("/" if entry.is_dir() else "")
        for entry in path.iterdir()
        if entry.name.casefold() != ".git"
    )
    return ToolResult(ok=True, content="\n".join(names))


async def _run(ctx: ToolContext, args: RunInput) -> ToolResult:
    cwd = resolve_in_worktree(ctx.worktree, args.cwd) if args.cwd is not None else None
    try:
        result = await ctx.exec_runner.run(args.argv, cwd=cwd)
    except ExecRefusedError as exc:
        return _harness_error(f"command refused: {exc}")
    if result.denied:
        return _harness_error("command denied by the approval policy; nothing was run")
    status = "timed out and was killed" if result.timed_out else f"exit code: {result.exit_code}"
    content = f"{status}\n--- stdout (tail) ---\n{result.stdout_tail}\n--- stderr (tail) ---\n"
    content += result.stderr_tail
    return ToolResult(ok=result.exit_code == 0 and not result.timed_out, content=content)


# --- registry ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Tool:
    spec: ToolSpec
    input_model: type[_Input]
    handler: Callable[[ToolContext, Any], ToolResult | Awaitable[ToolResult]]


def _schema(model: type[_Input]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema["additionalProperties"] = False
    return schema


def _tool(name: str, description: str, model: type[_Input], handler: Any) -> _Tool:
    return _Tool(
        ToolSpec(name=name, description=description, input_schema=_schema(model)), model, handler
    )


_TOOLS: dict[str, _Tool] = {
    tool.spec.name: tool
    for tool in [
        _tool("read_file", "Read a UTF-8 text file in the worktree.", ReadFileInput, _read_file),
        _tool(
            "write_file",
            "Create or overwrite a file in the worktree (parent folders are created).",
            WriteFileInput,
            _write_file,
        ),
        _tool(
            "edit_file",
            "Replace one exact occurrence of 'old' with 'new' in a worktree file.",
            EditFileInput,
            _edit_file,
        ),
        _tool(
            "list_dir",
            "List a folder in the worktree; folders end with '/'.",
            ListDirInput,
            _list_dir,
        ),
        _tool(
            "run",
            "Run a command (argv list, no shell) in the worktree. May need user approval.",
            RunInput,
            _run,
        ),
    ]
}

TOOL_SPECS: list[ToolSpec] = [tool.spec for tool in _TOOLS.values()]


async def dispatch(ctx: ToolContext, name: str, tool_input: dict[str, Any]) -> ToolResult:
    """Validate `tool_input` and run tool `name`. Never raises for model mistakes."""
    tool = _TOOLS.get(name)
    if tool is None:
        return _harness_error(f"unknown tool {name!r}; available: {', '.join(_TOOLS)}")
    try:
        args = tool.input_model.model_validate(tool_input)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors()
        )
        return _harness_error(f"invalid input for {name}: {details}")
    try:
        outcome = tool.handler(ctx, args)
        return await outcome if isinstance(outcome, Awaitable) else outcome
    except PathOutsideWorktreeError as exc:
        return _harness_error(str(exc))
    except OSError as exc:
        return _harness_error(f"{name} failed: {exc.strerror or exc}")
