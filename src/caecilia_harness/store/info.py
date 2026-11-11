"""Store metadata that other packages may read without touching sqlite3 directly."""

from __future__ import annotations

import sqlite3
from importlib.resources import files

MIN_SQLITE = (3, 35)  # WAL + RETURNING


def sqlite_version() -> str:
    return sqlite3.sqlite_version


def sqlite_supported() -> bool:
    return sqlite3.sqlite_version_info >= MIN_SQLITE


def schema_sql() -> str:
    """Return the version-1 schema shipped with the package."""
    return files("caecilia_harness.store").joinpath("schema.sql").read_text(encoding="utf-8")
