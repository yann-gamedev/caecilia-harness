"""Command-line entry point: `caecilia`.

Commands for M1 (run, status, cancel, approve, deny) are added by backlog task M1-09.
"""

from __future__ import annotations

import typer

from caecilia_harness import __version__
from caecilia_harness.interfaces.doctor import run_all

app = typer.Typer(
    name="caecilia",
    help="Caecilia Harness: AI orchestration harness.",
    no_args_is_help=True,
    add_completion=False,
)

_MARK = {"ok": "[ok]  ", "warn": "[warn]", "fail": "[FAIL]"}


@app.command()
def version() -> None:
    """Print the harness version."""
    typer.echo(__version__)


@app.command()
def doctor() -> None:
    """Check Python, Git, SQLite, sandbox support, and the data directory."""
    checks = run_all()
    for check in checks:
        typer.echo(f"{_MARK[check.level]} {check.name:<8} {check.detail}")
    if any(check.level == "fail" for check in checks):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
