"""FakeWorker: one file per task inside its write set, and per-task failures (backlog M2-02)."""

from __future__ import annotations

from pathlib import Path

import pytest

from caecilia_harness.core.models import Budget, ContextPack, TaskSpec, Workspace
from caecilia_harness.workers.fake import DEFAULT_FILE, FakeWorker, default_file


def _spec(task: str, write_set: list[str]) -> TaskSpec:
    return TaskSpec(
        id=f"r1-{task}", run_id="r1", title=task, instructions=f"do {task}", write_set=write_set
    )


@pytest.mark.parametrize(
    ("write_set", "expected"),
    [
        ([], DEFAULT_FILE),
        (["**"], DEFAULT_FILE),
        (["docs/notes.md", "docs/**"], "docs/notes.md"),
        (["api/**", "tests/api/**"], "api/caecilia_fake_api.md"),
        (["src/*.py"], "src/caecilia_fake_api.md"),
        (["*.md"], "caecilia_fake_api.md"),
        (["a/b/**/c"], "a/b/caecilia_fake_api.md"),
    ],
)
def test_default_file_stays_inside_the_write_set(write_set: list[str], expected: str) -> None:
    assert default_file(_spec("api", write_set)) == expected


async def _run(worker: FakeWorker, spec: TaskSpec, worktree: Path) -> tuple[bool, str | None]:
    ws = Workspace(task_id=spec.id, path=str(worktree), branch="b", base_commit="c")
    handle = await worker.start(spec, ContextPack(task=spec), ws, Budget())
    result = await worker.result(handle)
    return result.success, result.error


async def test_writes_its_default_file(tmp_path: Path) -> None:
    ok, _ = await _run(FakeWorker(), _spec("api", ["api/**"]), tmp_path)
    assert ok
    assert (tmp_path / "api" / "caecilia_fake_api.md").read_text(encoding="utf-8") == "do api\n"


async def test_fail_for_only_fails_the_named_task(tmp_path: Path) -> None:
    worker = FakeWorker(fail_for={"api": "permanent"})
    assert await _run(worker, _spec("api", ["api/**"]), tmp_path / "a") == (False, "permanent")
    assert await _run(worker, _spec("web", ["web/**"]), tmp_path / "w") == (True, None)
    # A plan id that merely ends the same way is a different task.
    assert await _run(worker, _spec("myapi", ["x/**"]), tmp_path / "m") == (True, None)


async def test_fail_with_applies_to_every_task(tmp_path: Path) -> None:
    worker = FakeWorker(fail_with="transient", fail_for={"api": "permanent"})
    assert await _run(worker, _spec("api", ["api/**"]), tmp_path / "a") == (False, "permanent")
    assert await _run(worker, _spec("web", ["web/**"]), tmp_path / "w") == (False, "transient")
