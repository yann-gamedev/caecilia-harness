"""Single-writer lock (backlog M1-11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from caecilia_harness.store.lock import WriterLock, WriterLockedError


def test_second_lock_on_same_path_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "home" / "harness.lock"
    with WriterLock(path) as first:
        assert first.held
        second = WriterLock(path)
        with pytest.raises(WriterLockedError, match="orchestrator lain sedang berjalan"):
            second.acquire()
        assert not second.held
    # Released by the context manager: it can be taken again.
    with WriterLock(path) as again:
        assert again.held


def test_release_is_idempotent(tmp_path: Path) -> None:
    lock = WriterLock(tmp_path / "harness.lock")
    lock.acquire()
    lock.acquire()  # already held: no-op
    lock.release()
    lock.release()
    assert not lock.held


def test_different_paths_do_not_conflict(tmp_path: Path) -> None:
    with WriterLock(tmp_path / "a.lock"), WriterLock(tmp_path / "b.lock"):
        pass
