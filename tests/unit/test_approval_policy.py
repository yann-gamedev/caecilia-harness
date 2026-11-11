"""Exec approval policy (backlog M1-06)."""

from __future__ import annotations

from pathlib import Path

import pytest

from caecilia_harness.security.approval import ApprovalPolicy, ExecRequest


def _req(*argv: str) -> ExecRequest:
    return ExecRequest(argv=list(argv), cwd=Path("wt"), task_id="t1")


def test_default_is_ask() -> None:
    assert ApprovalPolicy().decide(_req("ls")) == "ask"


@pytest.mark.parametrize("mode", ["allow", "ask", "deny"])
def test_mode_applies_without_remembered_prefix(mode: str) -> None:
    assert ApprovalPolicy(mode=mode).decide(_req("ls")) == mode  # type: ignore[arg-type]


def test_remembered_prefix_allows() -> None:
    policy = ApprovalPolicy(remembered={("uv", "run", "pytest")})
    assert policy.decide(_req("uv", "run", "pytest", "-q")) == "allow"
    assert policy.decide(_req("uv", "run", "pytest")) == "allow"
    assert policy.decide(_req("uv", "run", "ruff")) == "ask"
    assert policy.decide(_req("uv", "run")) == "ask"


def test_deny_mode_wins_over_remembered() -> None:
    policy = ApprovalPolicy(mode="deny", remembered={("ls",)})
    assert policy.decide(_req("ls")) == "deny"


def test_empty_prefix_never_matches() -> None:
    assert ApprovalPolicy(remembered={()}).decide(_req("rm", "-rf", "x")) == "ask"


def test_request_is_frozen() -> None:
    req = _req("ls")
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        req.task_id = "other"  # type: ignore[misc]
