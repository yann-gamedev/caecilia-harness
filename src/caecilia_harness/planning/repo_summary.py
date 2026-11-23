"""What the planner may know about a repo: its tracked file list and the start of its README.

Nothing else is read. The whole summary is marked as untrusted data, because a README (or a
file name) can contain text that looks like instructions.
"""

from __future__ import annotations

from pathlib import Path

from caecilia_harness.security.untrusted import wrap_untrusted
from caecilia_harness.workspace.git import run_git


def _readme_name(files: list[str]) -> str | None:
    """The top-level README, preferring README.md, then any README* file."""
    top_level = [name for name in files if "/" not in name and name.upper().startswith("README")]
    for name in sorted(top_level, key=lambda n: (n.upper() != "README.MD", n)):
        return name
    return None


def _read_start(path: Path, limit: int) -> tuple[str, bool]:
    with path.open("rb") as fh:
        data = fh.read(limit * 4 + 4)  # enough bytes for `limit` characters of UTF-8
    text = data.decode("utf-8", errors="replace")
    return text[:limit], len(text) > limit or path.stat().st_size > len(data)


async def summarize_repo(repo: Path, max_files: int = 400, max_readme_chars: int = 4000) -> str:
    """Tracked files (`git ls-files`, cut off with a marker) plus the start of the README."""
    listed = await run_git(["ls-files", "-z"], repo)
    files = [name for name in listed.stdout.decode("utf-8", errors="replace").split("\0") if name]

    lines = [f"Tracked files ({len(files)}):", *files[:max_files]]
    if len(files) > max_files:
        lines.append(f"[... {len(files) - max_files} more files not shown]")

    readme = _readme_name(files)
    if readme is not None:
        text, cut = _read_start(repo / readme, max_readme_chars)
        lines += ["", f"Start of {readme}:", text.rstrip()]
        if cut:
            lines.append(f"[... {readme} truncated at {max_readme_chars} characters]")
    return wrap_untrusted("\n".join(lines), "repo-summary")
