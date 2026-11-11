"""Open the state database and bring its schema up to date (ADR-0002)."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from caecilia_harness.store.errors import MigrationError, StoreError
from caecilia_harness.store.info import schema_sql

__all__ = [
    "Migration",
    "MigrationError",
    "StoreError",
    "apply_migrations",
    "migrations",
    "open_db",
    "open_readonly",
]

Migration = tuple[int, str]


def migrations() -> list[Migration]:
    """Return every known migration as (version, sql), in the order they must be applied."""
    return [(1, schema_sql())]


def open_db(path: Path) -> sqlite3.Connection:
    """Open the read-write database at `path`, creating it and applying pending migrations.

    Transactions are managed explicitly by callers (`BEGIN IMMEDIATE` / `COMMIT`), hence
    `isolation_level=None`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        apply_migrations(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing database for reading only (readers such as `caecilia status`)."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if exists is None:
        return set()
    return {row[0] for row in conn.execute("SELECT version FROM schema_version")}


def apply_migrations(conn: sqlite3.Connection, pending: Sequence[Migration] | None = None) -> int:
    """Apply every migration whose version is not recorded yet; return the current version.

    Each migration runs in its own transaction together with its `schema_version` row, so a
    failure leaves no half-created tables behind.
    """
    known = migrations() if pending is None else pending
    applied = _applied_versions(conn)
    for version, sql in sorted(known):
        if version in applied:
            continue
        try:
            # executescript never opens a transaction itself, so the BEGIN is part of the script.
            conn.executescript(f"BEGIN IMMEDIATE;\n{sql}")
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat()),
            )
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            # Another writer may have applied the same version while we waited for the lock.
            if version in _applied_versions(conn):
                continue
            raise MigrationError(f"migration {version} failed: {exc}") from exc
        applied.add(version)
    return max(applied, default=0)
