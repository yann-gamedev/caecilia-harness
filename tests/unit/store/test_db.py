"""Opening the state database and applying schema migrations (backlog M1-01)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from caecilia_harness.paths import db_path
from caecilia_harness.store.db import (
    MigrationError,
    StoreError,
    apply_migrations,
    open_db,
    open_readonly,
)
from caecilia_harness.store.info import schema_sql

EXPECTED_TABLES = {
    "runs",
    "tasks",
    "task_deps",
    "events",
    "documents",
    "context_pins",
    "artifacts",
    "leases",
    "approvals",
    "outbox",
    "schema_version",
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


def _versions(conn: sqlite3.Connection) -> list[int]:
    return [row[0] for row in conn.execute("SELECT version FROM schema_version")]


def test_new_db_has_all_tables_and_version_1() -> None:
    with closing(open_db(db_path())) as conn:
        assert _tables(conn) >= EXPECTED_TABLES
        assert _versions(conn) == [1]
        applied_at = conn.execute("SELECT applied_at FROM schema_version").fetchone()[0]
        assert applied_at.endswith("+00:00")


def test_reopen_is_idempotent() -> None:
    with closing(open_db(db_path())):
        pass
    with closing(open_db(db_path())) as conn:
        assert _versions(conn) == [1]
        assert apply_migrations(conn) == 1
        assert _versions(conn) == [1]


def test_pragmas() -> None:
    with closing(open_db(db_path())) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.isolation_level is None
        assert conn.row_factory is sqlite3.Row


def test_missing_parent_folder_is_created(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "harness.db"
    with closing(open_db(path)):
        pass
    assert path.is_file()


def test_readonly_rejects_writes() -> None:
    with closing(open_db(db_path())):
        pass
    with closing(open_readonly(db_path())) as conn:
        assert _versions(conn) == [1]
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO schema_version VALUES (2, 'x')")


def test_failed_migration_leaves_nothing_behind(tmp_path: Path) -> None:
    path = tmp_path / "broken.db"
    broken = [(1, schema_sql() + "\nCREATE TABLE half_done (;\n")]
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        with pytest.raises(MigrationError):
            apply_migrations(conn, broken)
        assert not conn.in_transaction
        assert _tables(conn) <= {"schema_version"}
        if "schema_version" in _tables(conn):
            assert _versions(conn) == []
    assert issubclass(MigrationError, StoreError)


def test_migrations_apply_in_order_and_only_once(tmp_path: Path) -> None:
    path = tmp_path / "multi.db"
    migrations = [
        (1, schema_sql()),
        (2, "CREATE TABLE extra (x INTEGER);"),
    ]
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        assert apply_migrations(conn, migrations[:1]) == 1
        assert apply_migrations(conn, migrations) == 2
        assert apply_migrations(conn, migrations) == 2
        assert _versions(conn) == [1, 2]
        assert "extra" in _tables(conn)
