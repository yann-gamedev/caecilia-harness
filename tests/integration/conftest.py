from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def git(repo: Path, *args: str) -> str:
    """Run git synchronously in `repo` for test setup and assertions."""
    exe = shutil.which("git")
    assert exe is not None
    done = subprocess.run(
        [exe, *args], cwd=repo, capture_output=True, text=True, check=True, encoding="utf-8"
    )
    return done.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A throwaway repository (path contains a space) with one commit and a `.gitignore`."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "my repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (repo / "old.txt").write_text("remove me\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo
