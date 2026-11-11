"""Content-addressed artifact store (backlog M1-05)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from caecilia_harness.artifacts import ArtifactNotFoundError, put_bytes, read_bytes
from caecilia_harness.paths import artifacts_dir


def _files() -> list[Path]:
    return [p for p in artifacts_dir().rglob("*") if p.is_file()]


def test_put_returns_sha256_and_round_trips() -> None:
    data = b"diff --git a/x b/x\n\x00binary"
    digest = put_bytes(data, "patch")
    assert digest == hashlib.sha256(data).hexdigest()
    assert read_bytes(digest) == data
    assert _files() == [artifacts_dir() / digest[:2] / digest]


def test_same_content_twice_is_one_file() -> None:
    first = put_bytes(b"same", "patch")
    second = put_bytes(b"same", "log")
    assert first == second
    assert len(_files()) == 1


def test_empty_content_is_allowed() -> None:
    assert read_bytes(put_bytes(b"", "patch")) == b""


def test_missing_artifact() -> None:
    with pytest.raises(ArtifactNotFoundError):
        read_bytes("0" * 64)


@pytest.mark.parametrize("bad", ["../etc/passwd", "ABC", "a" * 63, "g" * 64])
def test_rejects_non_digest(bad: str) -> None:
    with pytest.raises(ValueError):
        read_bytes(bad)
