"""Guardrails on the scaffold itself: contracts load, boundaries hold, CLI works."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from typer.testing import CliRunner

from caecilia_harness import __version__
from caecilia_harness.core import models, ports
from caecilia_harness.interfaces.cli import app
from caecilia_harness.paths import db_path, harness_home
from caecilia_harness.store.info import schema_sql

SRC = Path(__file__).resolve().parents[2] / "src" / "caecilia_harness"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_only_store_imports_sqlite3() -> None:
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if "sqlite3" in _imported_modules(path) and path.parent.name != "store"
    ]
    assert offenders == []


def test_ports_are_protocols() -> None:
    for name in ("ModelProvider", "WorkerAdapter", "WorkspaceBackend", "Gate", "StateStore"):
        assert getattr(getattr(ports, name), "_is_protocol", False), name


def test_models_are_frozen_and_strict() -> None:
    spec = models.TaskSpec(id="t1", run_id="r1", title="t", instructions="do it")
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError
        spec.title = "changed"  # type: ignore[misc]
    with pytest.raises(Exception):  # noqa: B017
        models.TaskSpec(id="t1", run_id="r1", title="t", instructions="x", unknown=1)  # type: ignore[call-arg]


def test_schema_ships_with_package() -> None:
    sql = schema_sql()
    for table in ("events", "tasks", "documents", "context_pins", "leases", "outbox"):
        assert f"CREATE TABLE {table}" in sql


def test_home_follows_env(isolated_home: Path) -> None:
    assert harness_home() == isolated_home
    assert db_path().parent == isolated_home


def test_cli_version_and_doctor() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output

    result = runner.invoke(app, ["doctor"])
    assert "python" in result.output
    assert "git" in result.output
