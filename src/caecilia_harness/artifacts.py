"""Content-addressed file store (sha256) under `artifacts_dir()`.

Registering artifacts in the `artifacts` table is the orchestrator's job; this module only
stores and reads bytes.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from caecilia_harness.paths import artifacts_dir

_HASH = re.compile(r"[0-9a-f]{64}")


class ArtifactNotFoundError(KeyError):
    """No artifact is stored under the requested hash."""


def artifact_path(digest: str) -> Path:
    """Return where the artifact with sha256 hex `digest` lives (it may not exist)."""
    if not _HASH.fullmatch(digest):
        raise ValueError(f"not a sha256 hex digest: {digest!r}")
    return artifacts_dir() / digest[:2] / digest


def put_bytes(data: bytes, kind: str) -> str:
    """Store `data` and return its sha256 hex digest. Storing the same bytes again is a no-op.

    `kind` (e.g. "patch") is recorded by the orchestrator when it registers the artifact.
    """
    digest = hashlib.sha256(data).hexdigest()
    target = artifact_path(digest)
    if target.exists():
        return digest
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file in the same folder, then rename, so readers never see a partial file.
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{digest[:8]}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return digest


def read_bytes(digest: str) -> bytes:
    """Return the bytes stored under `digest`."""
    path = artifact_path(digest)
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise ArtifactNotFoundError(digest) from exc
